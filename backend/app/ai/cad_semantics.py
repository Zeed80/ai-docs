"""Stage 3 of the 3D-first redraw: the semantic layer bound to real geometry.

A drawing is not its shape. ``Ø80js6`` and ``Ø80`` are the same solid and
different instructions to the shop, and ``Ra 1,6`` is a property of one surface,
not of the part in general. This module binds what the reader saw on the sheet
to the edges the kernel actually built, addressed by the stable keys the B-Rep
report already emits.

That binding is what later stages need: a CAM plan asks "which surface carries
which fit and roughness", and a process sheet asks "which diameter is finished
to what". Neither question can be answered by geometry alone or by free text
alone — only by the pair.

Nothing is bound by guess. A value that matches no feature is reported as
unmatched rather than attached to the nearest thing, because a tolerance on the
wrong diameter is worse than a tolerance nobody applied.
"""

from __future__ import annotations

import math
import re
from typing import Any

# "Ø80js6", "80h11", "M75x1,5", "Ra 1,6", "HRC 58...62"
_DIAMETER_PREFIX = ("Ø", "⌀", "D", "d", "φ", "ф")
_NOMINAL = re.compile(r"-?\d+(?:[.,]\d+)?")
_FIT = re.compile(r"(?<=\d)\s*([A-Za-zА-Яа-я]{1,3}\d{1,2}(?:/[A-Za-z]{1,3}\d{1,2})?)")
_DEVIATION = re.compile(r"[+\-±]\s*\d+(?:[.,]\d+)?")
_THREAD = re.compile(r"\bM\s?(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
# Callouts that carry a number but do NOT measure a length: binding "Ra 1,6" to
# a 1.6 mm edge or "конус 7:24" to a 7 mm one would attach a surface property to
# an unrelated feature — the precise failure this module exists to prevent.
# Latin abbreviations need a word boundary (so "HB" does not swallow "HB12"),
# Russian stems must NOT (the boundary never fires inside "твёрдость").
_NOT_A_DIMENSION = re.compile(
    r"^\s*(?:(?:Ra|Rz|Rmax|HRC|HRB|HB|HV|Sh|IT\d)\b"
    r"|(?:шероховат|тверд|твёрд|конус|масс|масштаб)"
    r"|(?:taper|mass|scale)\b)",
    re.IGNORECASE,
)
# A ratio callout (7:24, 1:5) is a slope, not a size.
_RATIO = re.compile(r"\d\s*:\s*\d")

# Matching window: the same 0.5% used everywhere else in this pipeline.
_TOLERANCE_RATIO = 0.005
_TOLERANCE_FLOOR = 0.05


def parse_dimension(text: str) -> dict[str, Any] | None:
    """Split a drawing callout into what it measures and how tightly.

    Returns ``None`` for text that carries no nominal at all (a bare note), so
    the caller never tries to bind prose to geometry.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if _NOT_A_DIMENSION.search(raw) or _RATIO.search(raw):
        return None
    match = _NOMINAL.search(raw)
    if match is None:
        return None
    nominal = float(match.group().replace(",", "."))
    if nominal <= 0:
        return None
    thread = _THREAD.search(raw)
    # "M75x1,5" — the pitch must not be mistaken for a fit class.
    fit = None if thread else _FIT.search(raw)
    deviation = _DEVIATION.search(raw)
    return {
        "text": raw,
        "nominal_mm": nominal,
        "is_diameter": raw.lstrip()[:1] in _DIAMETER_PREFIX or bool(thread),
        # A fit letter+grade (js6, h11) and an explicit deviation (-0,019) are
        # different notations for the same intent; both are kept verbatim.
        "fit": fit.group(1) if fit else None,
        "deviation": deviation.group(0) if deviation else None,
        "thread": (
            f"M{thread.group(1).replace(',', '.')}x{thread.group(2).replace(',', '.')}"
            if thread else None
        ),
    }


def _edge_measures(report: dict) -> list[dict[str, Any]]:
    """Every model edge with the quantity a drawing would dimension it by."""
    measures: list[dict[str, Any]] = []
    for edge in report.get("edges") or []:
        length = float(edge.get("length_mm") or 0.0)
        if length <= 0:
            continue
        if edge.get("curve") == "Circle":
            measures.append({
                "key": edge.get("key"),
                "index": edge.get("index"),
                "kind": "diameter",
                "value_mm": length / math.pi,
            })
        elif edge.get("curve") == "Line":
            measures.append({
                "key": edge.get("key"),
                "index": edge.get("index"),
                "kind": "length",
                "value_mm": length,
            })
    return measures


def _match_edges(
    measures: list[dict[str, Any]], nominal: float, kind: str
) -> list[dict[str, Any]]:
    window = max(_TOLERANCE_FLOOR, nominal * _TOLERANCE_RATIO)
    return [
        measure for measure in measures
        if measure["kind"] == kind and abs(measure["value_mm"] - nominal) <= window
    ]


def bind_spec_to_solid(spec: dict, report: dict) -> dict[str, Any]:
    """Bind read dimensions, fits, threads and notes to model edges.

    ``bindings`` carries what was proven against geometry; ``unmatched`` carries
    what the sheet stated and the model does not contain — which is a genuine
    review item, usually meaning the reader misread a value or the solid is
    missing a feature.
    """
    measures = _edge_measures(report)
    bindings: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    seen: set[str] = set()

    def consider(text: str, source: str, applies_to: str | None = None) -> None:
        parsed = parse_dimension(text)
        if parsed is None:
            return
        signature = f"{source}:{parsed['text']}"
        if signature in seen:
            return
        seen.add(signature)
        kind = "diameter" if parsed["is_diameter"] else "length"
        matches = _match_edges(measures, parsed["nominal_mm"], kind)
        record = {
            **parsed,
            "kind": kind,
            "source": source,
            "applies_to": applies_to or None,
            "edge_keys": [m["key"] for m in matches],
            "edge_indices": [m["index"] for m in matches],
        }
        (bindings if matches else unmatched).append(record)

    for dimension in spec.get("dimensions") or []:
        if isinstance(dimension, dict):
            consider(
                str(dimension.get("value") or ""),
                "dimensions",
                str(dimension.get("applies_to") or "") or None,
            )

    # Section notes carry the threads and fits the drafter already built into
    # geometry ("резьба M75x1,5", "конус 7:24").
    for body in [spec.get("main_view"), *(spec.get("parts") or [])]:
        if not isinstance(body, dict):
            continue
        for group in ("outer", "bore"):
            for section in body.get(group) or []:
                if not isinstance(section, dict):
                    continue
                note = str(section.get("note") or "")
                if note:
                    consider(note, f"section_note:{group}")

    return {
        "bindings": bindings,
        "unmatched": unmatched,
        "bound_count": len(bindings),
        "unmatched_count": len(unmatched),
        "edges_available": len(measures),
    }


# Roughness/hardness/material notes are properties of surfaces or of the whole
# part. Without per-surface reading they are honestly recorded at PART level
# rather than pinned to an arbitrary face.
_SURFACE_NOTE_KINDS = ("roughness", "hardness", "material", "thread", "tolerance")


def collect_part_properties(spec: dict, report: dict) -> dict[str, Any]:
    """Part-level properties a CAM or process plan needs, with their source.

    Everything here is either measured from the solid or read from the sheet —
    nothing is derived from assumptions about the material or the process.
    """
    title = spec.get("title_block") or {}
    notes: dict[str, list[str]] = {}
    for annotation in spec.get("annotations") or []:
        if not isinstance(annotation, dict):
            continue
        kind = str(annotation.get("kind") or "other")
        text = str(annotation.get("text") or "").strip()
        if text and kind in _SURFACE_NOTE_KINDS:
            notes.setdefault(kind, []).append(text)

    bounds = report.get("bounds_mm") or {}
    return {
        "material": str(title.get("material") or "") or None,
        "designation": str(title.get("designation") or "") or None,
        "volume_mm3": report.get("volume_mm3"),
        "surface_area_mm2": report.get("surface_area_mm2"),
        "stock_envelope_mm": {
            "length": bounds.get("z"),
            "width": bounds.get("x"),
            "height": bounds.get("y"),
        },
        "notes": notes,
        # Bar stock for a lathe part: the smallest round bar the part fits in.
        "round_stock_diameter_mm": (
            round(max(float(bounds.get("x") or 0.0), float(bounds.get("y") or 0.0)), 3)
            or None
        ),
    }
