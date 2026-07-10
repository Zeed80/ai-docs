"""Cross-checks that decide between competing VLM reading hypotheses
(Ф4.2) — the piece of the "hypothesis manager" that the critique insisted
on: a VLM proposes readings, it does NOT get to pick the winner itself.
Only a check independent of the VLM call (geometry, standard series,
dimension-chain arithmetic) may promote a reading past ``inferred``.

Entry points:
    apply_vlm_readings   — attach Ф4.1's ranked readings to an entity
    resolve_hypotheses    — run cross-checks over a whole IR, promote or
                             queue-for-review each ambiguous entity
"""

from __future__ import annotations

import math

import structlog

from app.ai.cad_ir.assurance import set_assurance
from app.ai.cad_ir.schema import Alternative, CadIR, Circle, DimensionEntity, ReviewItem, Segment, TextEntity
from app.ai.techdraw_reference import STANDARD_RA_SERIES, is_valid_tolerance_symbol, nearest_ra, parse_thread

logger = structlog.get_logger()

# A candidate must lead the runner-up by at least this much (on the combined
# confidence+cross-check score) to be promoted without human confirmation —
# a narrow win is exactly the "unresolved ambiguity" case that must reach review.
_DECISIVE_MARGIN = 0.20
_GEOMETRY_MATCH_TOLERANCE = 0.12  # 12% relative — OCR/VLM digit misreads are usually >>12% off
_RA_MATCH_TOLERANCE = 0.03  # relative, catches "close enough to the standard series" reads


def apply_vlm_readings(entity: TextEntity | DimensionEntity, readings: list[dict]) -> None:
    """Attach Ф4.1 readings to an entity: highest-confidence reading becomes
    the entity's own text/value, the rest become ``alternatives``. Always
    lands at ``origin="vlm"``/``assurance="inferred"`` — cross-checks decide
    promotion, never this function."""
    if not readings:
        return
    leading = readings[0]
    entity.text = leading["text"]
    entity.origin = "vlm"
    entity.confidence = leading["confidence"]
    if isinstance(entity, DimensionEntity) and leading.get("value_mm") is not None:
        entity.value_mm = leading["value_mm"]
        entity.tolerance = leading.get("tolerance")
    entity.alternatives = [
        Alternative(value=r["text"], p=r["confidence"]) for r in readings[1:]
    ]
    entity.evidence = [*entity.evidence, f"vlm:kind={leading.get('kind', 'unclear')}"]


def _candidates(entity: TextEntity | DimensionEntity) -> list[tuple[str, float]]:
    """(text, confidence) for the leading reading + every alternative."""
    out = [(entity.text or "", entity.confidence)]
    for alt in entity.alternatives:
        if alt.value:
            out.append((alt.value, alt.p))
    return out


import re as _re

_NUMERIC_RE = _re.compile(r"(\d+(?:[.,]\d+)?)")


def _extract_numeric_mm(text: str) -> float | None:
    """Best-effort numeric value out of an arbitrary reading ("Ø18H7" -> 18.0,
    "36" -> 36.0, "M18" -> 18.0 too — thread scoring uses parse_thread
    separately so a thread's own "diameter-shaped" number doesn't matter)."""
    m = _NUMERIC_RE.search(text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "."))
    except ValueError:
        return None


def _thread_bonus(text: str) -> float:
    return 0.3 if parse_thread(text) is not None else 0.0


def _ra_bonus(value_mm: float | None, text: str) -> float:
    if "ra" not in text.lower() and value_mm is None:
        return 0.0
    try:
        val = value_mm if value_mm is not None else float(
            "".join(c for c in text if c.isdigit() or c in ".,").replace(",", ".")
        )
    except ValueError:
        return 0.0
    if val <= 0:
        return 0.0
    return 0.25 if abs(nearest_ra(val) - val) / val <= _RA_MATCH_TOLERANCE else 0.0


def _tolerance_bonus(tolerance: str | None) -> float:
    if not tolerance:
        return 0.0
    return 0.2 if is_valid_tolerance_symbol(tolerance) else -0.3


def _nearest_circle_diameter_mm(entity, ir: CadIR) -> float | None:
    """Diameter of the geometrically closest Circle to ``entity``'s anchor
    point, in mm — the ground truth a "Ø.." reading should agree with."""
    if ir.scale is None:
        return None
    anchor = entity.position if isinstance(entity, TextEntity) else entity.p1
    if anchor is None:
        return None
    best: tuple[float, Circle] | None = None
    for other in ir.entities:
        if not isinstance(other, Circle):
            continue
        d = math.hypot(other.center.x - anchor.x, other.center.y - anchor.y)
        if best is None or d < best[0]:
            best = (d, other)
    if best is None:
        return None
    return 2 * best[1].radius * ir.scale


def _geometry_bonus(value_mm: float | None, entity, ir: CadIR) -> float:
    if value_mm is None:
        return 0.0
    measured = _nearest_circle_diameter_mm(entity, ir)
    if measured is None or measured <= 0:
        return 0.0
    return 0.35 if abs(measured - value_mm) / measured <= _GEOMETRY_MATCH_TOLERANCE else 0.0


def _score_candidate(reading_text: str, tolerance: str | None,
                      confidence: float, entity, ir: CadIR) -> float:
    # Each candidate is scored against ITS OWN numeric reading — Ø18 and Ø16
    # predict different geometry, so they cannot share one value_mm the way
    # an entity-level field would suggest.
    value_mm = _extract_numeric_mm(reading_text)
    score = confidence
    score += _thread_bonus(reading_text)
    score += _ra_bonus(value_mm, reading_text)
    score += _tolerance_bonus(tolerance)
    score += _geometry_bonus(value_mm, entity, ir)
    return score


def resolve_hypotheses(ir: CadIR) -> None:
    """Cross-check every entity with VLM alternatives; promote a decisive
    winner to ``constraint_validated``, otherwise leave ``inferred`` and
    make sure it's queued for human review (with the alternatives visible
    in the UI — this is exactly the "review with variants" the plan calls
    for, not a silent pick)."""
    resolved_ids = {r.entity_id for r in ir.review}
    for entity in ir.entities:
        if entity.type not in ("text", "dimension") or not entity.alternatives:
            continue
        if entity.assurance == "human_approved":
            continue

        candidates = _candidates(entity)
        # Tolerance is a separate field only DimensionEntity carries and only
        # the LEADING reading currently populates it (Ф4.1) — applied to
        # every candidate's score for now; per-alternative tolerances are a
        # Ф4.1 extension, not a Ф4.2 scoring gap.
        tolerance = entity.tolerance if isinstance(entity, DimensionEntity) else None
        scored = sorted(
            (
                (_score_candidate(text, tolerance, conf, entity, ir), text, conf)
                for text, conf in candidates
            ),
            key=lambda t: t[0],
            reverse=True,
        )
        winner_score, winner_text, _winner_conf = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else 0.0
        decisive = (winner_score - runner_up_score) >= _DECISIVE_MARGIN

        if decisive and winner_text == entity.text:
            set_assurance(entity, "constraint_validated", actor="solver")
            logger.info(
                "hypothesis_resolved", entity_id=entity.id, text=winner_text,
                margin=round(winner_score - runner_up_score, 3),
            )
        else:
            # Either genuinely ambiguous, or the cross-checks favor an
            # ALTERNATIVE over the VLM's own top pick — either way a human
            # decides, the model doesn't get to overrule itself either.
            if entity.id not in resolved_ids:
                ir.review.append(ReviewItem(entity_id=entity.id, reason="unresolved_hypothesis"))
            logger.info(
                "hypothesis_ambiguous", entity_id=entity.id,
                candidates=[c[0] for c in candidates],
                margin=round(winner_score - runner_up_score, 3),
            )


# ── Line-class hypotheses (Ф4.3) ─────────────────────────────────────────────
# Geometric alternatives use Alternative.entity (a partial-update dict), not
# .value — distinct code path from the text/dimension one above, whose
# cross-checks (thread tables, Ra series) don't apply to "is this an axis".

_LINE_DECISIVE_MARGIN = 0.20
_AXIS_THROUGH_CIRCLE_BONUS = 0.35
_AXIS_CENTER_TOL_PX = 6.0


def apply_line_hypotheses(entity, result: dict) -> None:
    """Attach Ф4.3's line-classification result to a Segment/Polyline:
    leading reading becomes the entity's own line_class, the rest become
    geometric ``alternatives``. A detected symbol (roughness/thread/weld/
    datum) is recorded as evidence only — Ф4.4/normcontrol turn it into a
    real entity later; this module only manages the line_class hypothesis."""
    readings = result.get("line_readings") or []
    if readings:
        leading = readings[0]
        entity.line_class = leading["line_class"]
        entity.confidence = leading["confidence"]
        entity.origin = "vlm"
        entity.alternatives = [
            Alternative(entity={"line_class": r["line_class"]}, p=r["confidence"])
            for r in readings[1:]
        ]
    symbol = result.get("symbol")
    if symbol:
        entity.evidence = [
            *entity.evidence,
            f"vlm_symbol:{symbol['kind']}={symbol.get('text', '')}@{symbol['confidence']:.2f}",
        ]


def _point_near_segment(pt, p1, p2, tol_px: float) -> bool:
    """Perpendicular distance from ``pt`` to the segment p1-p2 (clamped to
    the segment's own span, with a small overhang allowance — axis lines
    conventionally extend a bit past the feature they center on)."""
    dx, dy = p2.x - p1.x, p2.y - p1.y
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        return math.hypot(pt.x - p1.x, pt.y - p1.y) <= tol_px
    t = ((pt.x - p1.x) * dx + (pt.y - p1.y) * dy) / length_sq
    t_clamped = max(-0.15, min(1.15, t))  # 15% overhang allowance each side
    proj_x, proj_y = p1.x + t_clamped * dx, p1.y + t_clamped * dy
    return math.hypot(pt.x - proj_x, pt.y - proj_y) <= tol_px


def _axis_bonus_for_class(line_class: str, entity, ir: CadIR) -> float:
    if line_class != "axis" or not isinstance(entity, Segment):
        return 0.0
    for other in ir.entities:
        if isinstance(other, Circle) and _point_near_segment(
            other.center, entity.p1, entity.p2, _AXIS_CENTER_TOL_PX
        ):
            return _AXIS_THROUGH_CIRCLE_BONUS
    return 0.0


def resolve_line_hypotheses(ir: CadIR) -> None:
    """Cross-check geometric (line_class) hypotheses the same way
    ``resolve_hypotheses`` cross-checks text/dimension ones: a decisive
    winner is promoted to ``constraint_validated``; a close call is left
    ``inferred`` and queued for human review with its alternatives intact."""
    resolved_ids = {r.entity_id for r in ir.review}
    for entity in ir.entities:
        if entity.type not in ("segment", "polyline") or entity.assurance == "human_approved":
            continue
        geo_alts = [a for a in entity.alternatives if a.entity and "line_class" in a.entity]
        if not geo_alts:
            continue

        candidates = [(entity.line_class, entity.confidence)] + [
            (a.entity["line_class"], a.p) for a in geo_alts
        ]
        scored = sorted(
            (
                (conf + _axis_bonus_for_class(lc, entity, ir), lc, conf)
                for lc, conf in candidates
            ),
            key=lambda t: t[0],
            reverse=True,
        )
        winner_score, winner_lc, _ = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else 0.0
        decisive = (winner_score - runner_up_score) >= _LINE_DECISIVE_MARGIN

        if decisive and winner_lc == entity.line_class:
            set_assurance(entity, "constraint_validated", actor="solver")
            logger.info("line_hypothesis_resolved", entity_id=entity.id, line_class=winner_lc)
        else:
            if entity.id not in resolved_ids:
                ir.review.append(ReviewItem(entity_id=entity.id, reason="unresolved_hypothesis"))
            logger.info(
                "line_hypothesis_ambiguous", entity_id=entity.id,
                candidates=[c[0] for c in candidates],
            )


def check_dimension_chains(ir: CadIR) -> list[str]:
    """Ported from ``drawing_validator._check_dimension_chains`` to IR-native
    ``DimensionEntity`` — partial linear dimensions should sum to the overall
    span within ±0.5%; only warn past a 5% delta (small legitimate mismatches
    are common — different chains, rounding)."""
    warnings: list[str] = []
    linear = [e for e in ir.entities if isinstance(e, DimensionEntity) and e.kind == "linear" and e.value_mm]
    if len(linear) < 3:
        return warnings
    nominals = sorted(e.value_mm for e in linear)
    total = nominals[-1]
    if total <= 0:
        return warnings
    parts_sum = sum(nominals[:-1])
    rel_delta = abs(parts_sum - total) / total
    if rel_delta > 0.05:
        warnings.append(
            f"Сумма частичных размеров ({parts_sum:.2f}мм) не сходится с общим "
            f"({total:.2f}мм), расхождение {rel_delta:.1%} — проверьте размерную цепь"
        )
    return warnings
