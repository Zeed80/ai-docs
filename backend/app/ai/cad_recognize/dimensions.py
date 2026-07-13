"""Dimension reconstruction (B2): pair a numeric OCR label with the thin
dimension line it annotates, and emit a real ``DimensionEntity``.

Recognition backends only ever produce raw geometry (segments) plus OCR
``TextEntity`` labels floating on top. A размер on an ЕСКД sheet is
structurally a *dimension line* (thin, with arrowheads) carrying a *value*
(the OCR number, possibly with a Ø/R prefix). This pass rebuilds that
structure deterministically — no LLM — by matching each dimension-looking
label to the nearest suitable thin segment:

- the label text must parse as a dimension value (a number, optionally
  Ø/R/M-prefixed, optionally with a tolerance suffix);
- a candidate line must be *thin* (auxiliary width) and its midpoint close
  to the label, and roughly aligned with the label's own extent;
- when the parsed value and the line's measured length (via the sheet
  scale) disagree beyond a tolerance, the dimension still ships but is
  flagged low-confidence for review — never silently "corrected".

Matched segments are replaced by the DimensionEntity; unmatched labels and
lines are returned untouched. Conservative by design: anything ambiguous is
left as separate text + line for the human, exactly as before.
"""

from __future__ import annotations

import math
import re

import structlog

from app.ai.cad_ir.schema import (
    DimensionEntity,
    Entity,
    Point,
    Segment,
    TextEntity,
)

logger = structlog.get_logger()

# A dimension label: an optional kind prefix, a number (comma/dot decimal),
# and an optional tolerance/fit suffix (H7, ±0.1, -0.05, 8js6 …). Plain
# integers like sheet-zone letters are excluded by requiring the token to be
# *mostly* the number (see _parse_label).
_DIAMETER_PREFIX = ("⌀", "ø", "Ø", "d", "D")
_RADIUS_PREFIX = ("R", "r", "Р")
_VALUE_RE = re.compile(r"(-?\d+(?:[.,]\d+)?)")

# A thin (auxiliary) dimension line may be up to this fraction of the sheet's
# smaller side. A main/contour segment is only ever a dimension-line
# candidate when it is genuinely short (below _SHORT_LINE_FRACTION) AND the
# label sits right on it — the neural backend marks every segment "main", so
# a width-only gate would find nothing on a neural-won sheet, but eating a
# structural contour must stay very unlikely.
_MAX_DIM_LINE_FRACTION = 0.6
_SHORT_LINE_FRACTION = 0.3
# Perpendicular label→line distance, in multiples of the label's own height
# (dimension text sits on/just above its line). Thin lines get the looser
# bound; main segments must match tightly to be consumed.
_THIN_PERP_FACTOR = 2.0
_MAIN_PERP_FACTOR = 1.3
# Value vs measured-length agreement (relative) before we flag for review.
_VALUE_MISMATCH_REL = 0.15
_MISMATCH_CONFIDENCE = 0.4
_MATCH_CONFIDENCE = 0.85


def _parse_label(text: str) -> tuple[str, float | None, str | None] | None:
    """(kind, value_mm, tolerance) or None when the text is not a dimension.

    kind ∈ {linear, diameter, radial}. Rejects tokens that are not
    predominantly a number (avoids treating view letters 'А'/'Б' or notes as
    dimensions)."""
    raw = text.strip()
    if not raw:
        return None
    match = _VALUE_RE.search(raw.replace(" ", ""))
    if not match:
        return None
    number = match.group(1)
    # The number must be a substantial part of the label — a lone digit
    # inside a long note or a garbled OCR read ("Fa3pa0") is not a
    # dimension. A dimension token is digit-dominant: at most one more
    # letter than it has digits (allowing a single fit letter like H/js).
    digits = sum(ch.isdigit() for ch in raw)
    alphas = sum(ch.isalpha() for ch in raw)
    if digits == 0 or len(raw) > len(number) + 6 or alphas > digits + 1:
        return None
    try:
        value = float(number.replace(",", "."))
    except ValueError:
        return None
    if value <= 0:
        return None

    kind = "linear"
    head = raw.lstrip()
    if any(p in raw for p in _DIAMETER_PREFIX):
        kind = "diameter"
    elif head[:1] in _RADIUS_PREFIX:
        kind = "radial"

    # Tolerance/fit = whatever trails the number (H7, js6, ±0.1, -0.05).
    tail = raw[match.end():].strip() if match.end() < len(raw) else ""
    tolerance = tail or None
    return kind, value, tolerance


def _seg_len(s: Segment) -> float:
    return math.hypot(s.p2.x - s.p1.x, s.p2.y - s.p1.y)


def _point_to_segment(
    px: float, py: float, s: Segment
) -> tuple[float, float]:
    """(perpendicular distance, projection parameter t∈[0,1]) of a point
    onto a segment. t outside [0,1] means the point is beyond an end."""
    dx, dy = s.p2.x - s.p1.x, s.p2.y - s.p1.y
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        return math.hypot(px - s.p1.x, py - s.p1.y), 0.0
    t = ((px - s.p1.x) * dx + (py - s.p1.y) * dy) / length_sq
    t_clamped = max(0.0, min(1.0, t))
    fx = s.p1.x + t_clamped * dx
    fy = s.p1.y + t_clamped * dy
    return math.hypot(px - fx, py - fy), t


def _label_center(t: TextEntity) -> tuple[float, float]:
    if t.source_region is not None:
        r = t.source_region
        return ((r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0)
    return (t.position.x, t.position.y - t.height / 2.0)


def reconstruct_dimensions(
    entities: list[Entity],
    texts: list[TextEntity],
    scale: float | None,
    sheet_w: int,
    sheet_h: int,
) -> tuple[list[Entity], list[TextEntity], int]:
    """Return (geometry entities with dimensions substituted, remaining text
    entities, count of dimensions built). ``entities`` should be the
    recognized geometry (no text); ``texts`` the OCR labels."""
    short_side = min(sheet_w, sheet_h)
    candidates = [
        e
        for e in entities
        if isinstance(e, Segment)
        and _seg_len(e) <= _MAX_DIM_LINE_FRACTION * short_side
    ]
    if not candidates:
        return entities, texts, 0

    used_segment_ids: set[str] = set()
    used_text_ids: set[str] = set()
    dimensions: list[DimensionEntity] = []

    for label in texts:
        parsed = _parse_label(label.text)
        if parsed is None:
            continue
        kind, value_mm, tolerance = parsed
        lc = _label_center(label)
        height = max(label.height, 8.0)

        best: Segment | None = None
        best_dist = float("inf")
        for seg in candidates:
            if seg.id in used_segment_ids:
                continue
            is_thin = seg.width_class == "thin"
            # A main-width segment is only eligible when it's genuinely
            # short — a long contour edge is never a dimension line.
            if not is_thin and _seg_len(seg) > _SHORT_LINE_FRACTION * short_side:
                continue
            perp, t = _point_to_segment(lc[0], lc[1], seg)
            # The label must sit over the line (small perpendicular gap) and
            # within its span, not beyond an endpoint.
            if not (-0.15 <= t <= 1.15):
                continue
            reach = height * (_THIN_PERP_FACTOR if is_thin else _MAIN_PERP_FACTOR)
            if perp <= reach and perp < best_dist:
                best_dist = perp
                best = seg
        if best is None:
            continue

        measured_mm = _seg_len(best) * scale if scale else None
        confidence = _MATCH_CONFIDENCE
        evidence = [f"dim:label={label.id}", f"dim:line={best.id}"]
        if measured_mm and value_mm:
            rel = abs(measured_mm - value_mm) / max(value_mm, 1e-6)
            if rel > _VALUE_MISMATCH_REL:
                confidence = _MISMATCH_CONFIDENCE
                evidence.append(f"dim:mismatch={measured_mm:.1f}vs{value_mm:.1f}")

        used_segment_ids.add(best.id)
        used_text_ids.add(label.id)
        dimensions.append(
            DimensionEntity(
                kind=kind,  # type: ignore[arg-type]
                p1=Point(x=best.p1.x, y=best.p1.y),
                p2=Point(x=best.p2.x, y=best.p2.y),
                text=label.text.strip(),
                value_mm=value_mm,
                tolerance=tolerance,
                confidence=confidence,
                origin="cv",
                assurance="inferred",
                evidence=evidence,
            )
        )

    if not dimensions:
        return entities, texts, 0

    remaining_geometry = [
        e
        for e in entities
        if not (isinstance(e, Segment) and e.id in used_segment_ids)
    ]
    remaining_texts = [t for t in texts if t.id not in used_text_ids]
    logger.info("cad_dimensions_reconstructed", count=len(dimensions))
    return [*remaining_geometry, *dimensions], remaining_texts, len(dimensions)
