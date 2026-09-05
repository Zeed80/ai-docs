"""What to build when the sheet did not say — and how to admit it.

A reading with one hole in it used to stop everything: ``unresolved`` is
fail-closed, so a shaft missing one step length produced no part at all. That is
the wrong trade for a redraw. The person doing the work would rather have the
part with one dimension marked "assumed" and fix that dimension in the editor
than start from nothing — provided the assumption is VISIBLE. A quietly
invented number is worse than no part; a labelled one is a starting point.

So every rule here does three things:

* it computes a value only where there is a principled way to,
* it says which principle — arithmetic the sheet itself implies, or a standard
  that fixes the value (ГОСТ 10948 chamfers, 23360 keyways, 8724 thread pitch),
* and it records that the value is assumed, so the drawing can mark it, the
  review panel can list it, and the editor can offer it for correction.

Nothing here reads the drawing. It only completes what the reader could not.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Assumption:
    """One value the sheet did not give, and where it came from instead."""

    path: str
    field: str
    value: float
    rule: str
    origin: str = "assumed"  # "derived" when the sheet's own numbers force it

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "field": self.field,
            "value_mm": self.value,
            "rule": self.rule,
            "origin": self.origin,
        }


# ГОСТ 23360: parallel key width b and shaft groove depth t1, by shaft diameter.
_KEYWAY_BY_DIAMETER: tuple[tuple[float, float, float], ...] = (
    (22.0, 6.0, 3.5),
    (30.0, 8.0, 4.0),
    (38.0, 10.0, 5.0),
    (44.0, 12.0, 5.0),
    (50.0, 14.0, 5.5),
    (58.0, 16.0, 6.0),
    (65.0, 18.0, 7.0),
    (75.0, 20.0, 7.5),
    (85.0, 22.0, 9.0),
    (95.0, 25.0, 9.0),
    (110.0, 28.0, 10.0),
    (130.0, 32.0, 11.0),
)
# ГОСТ 8724: coarse pitch for a metric thread of this nominal diameter.
_COARSE_PITCH: dict[float, float] = {
    6.0: 1.0,
    8.0: 1.25,
    10.0: 1.5,
    12.0: 1.75,
    14.0: 2.0,
    16.0: 2.0,
    18.0: 2.5,
    20.0: 2.5,
    22.0: 2.5,
    24.0: 3.0,
    27.0: 3.0,
    30.0: 3.5,
    33.0: 3.5,
    36.0: 4.0,
    39.0: 4.0,
    42.0: 4.5,
    45.0: 4.5,
    48.0: 5.0,
    52.0: 5.0,
    56.0: 5.5,
    60.0: 5.5,
    64.0: 6.0,
    68.0: 6.0,
}
# ГОСТ 10948: the chamfer sizes a drawing actually uses, by the diameter it is
# cut on. Not a formula — a designer picks from the series, and so does this.
_CHAMFER_BY_DIAMETER: tuple[tuple[float, float], ...] = (
    (10.0, 0.6),
    (30.0, 1.0),
    (80.0, 1.6),
    (150.0, 2.0),
    (300.0, 2.5),
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _table_value(diameter: float, table: tuple[tuple[float, ...], ...]) -> tuple | None:
    for row in table:
        if diameter <= row[0]:
            return row
    return table[-1] if table else None


def _stated_overall(spec: dict) -> float | None:
    """The overall length the sheet stated, if it did — as a plain callout."""
    from app.ai.cad_recognize.spec_crosscheck import _stated_overall_length

    return _stated_overall_length(spec)


def apply_assumptions(spec: dict) -> tuple[dict, list[Assumption]]:
    """Complete the reading where a value can be justified, and say how.

    The spec is not mutated: the pair (read, completed) is what lets a reviewer
    see what was added. Values the sheet's own arithmetic forces are marked
    ``derived`` rather than ``assumed`` — a length that is the overall size
    minus every other step is not a guess, it is subtraction.
    """
    completed = copy.deepcopy(spec)
    assumptions: list[Assumption] = []

    bodies: list[tuple[str, dict]] = []
    main = completed.get("main_view")
    if isinstance(main, dict):
        bodies.append(("main_view", main))
    for index, part in enumerate(completed.get("parts") or []):
        if isinstance(part, dict):
            bodies.append((f"parts.{index}", part))

    overall = _stated_overall(completed)
    for body_path, body in bodies:
        _complete_lengths(body_path, body, overall, assumptions)
        _complete_features(body_path, body, assumptions)

    if assumptions:
        _record(completed, assumptions)
        logger.info(
            "cad_spec_assumptions",
            count=len(assumptions),
            fields=[item.path for item in assumptions],
        )
    return completed, assumptions


def _complete_lengths(
    body_path: str, body: dict, overall: float | None, assumptions: list[Assumption]
) -> None:
    """The one missing step length the overall size determines exactly.

    With every other length known and an overall stated, the remainder is
    arithmetic, not a guess — and it is the single most common hole in a read
    profile. Two or more missing lengths are underdetermined and must remain
    unresolved: labelling an invented split does not make its geometry true.
    """
    sections = [s for s in (body.get("outer") or []) if isinstance(s, dict)]
    if not sections:
        return
    missing = [index for index, s in enumerate(sections) if not _num(s.get("length_mm"))]
    if not missing:
        return
    known = sum(_num(s.get("length_mm")) or 0.0 for s in sections)
    if overall and overall > known and len(missing) == 1:
        remainder = overall - known
        index = missing[0]
        sections[index]["length_mm"] = round(remainder, 3)
        assumptions.append(
            Assumption(
                path=f"{body_path}.outer.{index}",
                field="length_mm",
                value=round(remainder, 3),
                rule=(
                    f"остаток габарита {overall:g} мм за вычетом прочитанных "
                    f"ступеней ({known:g} мм)"
                ),
                origin="derived",
            )
        )


def _complete_features(body_path: str, body: dict, assumptions: list[Assumption]) -> None:
    """Feature sizes the standards fix, when the sheet only named the feature."""
    sections = [s for s in (body.get("outer") or []) if isinstance(s, dict)]
    max_diameter = max((_num(s.get("diameter_mm")) or 0.0 for s in sections), default=0.0)

    for index, chamfer in enumerate(body.get("chamfers") or []):
        if not isinstance(chamfer, dict) or _num(chamfer.get("size_mm")):
            continue
        diameter = _num(chamfer.get("at_diameter_mm")) or max_diameter
        row = _table_value(diameter, _CHAMFER_BY_DIAMETER) if diameter else None
        if not row:
            continue
        chamfer["size_mm"] = row[1]
        chamfer.setdefault("angle_deg", 45.0)
        assumptions.append(
            Assumption(
                path=f"{body_path}.chamfers.{index}",
                field="size_mm",
                value=row[1],
                rule=f"ГОСТ 10948: фаска {row[1]:g}×45° для Ø{diameter:g} мм",
            )
        )

    for index, keyway in enumerate(body.get("keyways") or []):
        if not isinstance(keyway, dict):
            continue
        diameter = _num(keyway.get("at_diameter_mm")) or max_diameter
        row = _table_value(diameter, _KEYWAY_BY_DIAMETER) if diameter else None
        if not row:
            continue
        if not _num(keyway.get("width_mm")):
            keyway["width_mm"] = row[1]
            assumptions.append(
                Assumption(
                    path=f"{body_path}.keyways.{index}",
                    field="width_mm",
                    value=row[1],
                    rule=f"ГОСТ 23360: b={row[1]:g} мм для вала Ø{diameter:g} мм",
                )
            )
        if not _num(keyway.get("depth_mm")):
            keyway["depth_mm"] = row[2]
            assumptions.append(
                Assumption(
                    path=f"{body_path}.keyways.{index}",
                    field="depth_mm",
                    value=row[2],
                    rule=f"ГОСТ 23360: t1={row[2]:g} мм для вала Ø{diameter:g} мм",
                )
            )

    for index, section in enumerate(sections):
        thread = section.get("thread")
        if not isinstance(thread, dict) or _num(thread.get("pitch_mm")):
            continue
        nominal = _num(thread.get("nominal_diameter_mm")) or _num(section.get("diameter_mm"))
        if not nominal:
            continue
        pitch = _COARSE_PITCH.get(round(nominal, 1))
        if pitch is None:
            # Not in the series: a pitch cannot be assumed for a thread whose
            # nominal is not standard, and a wrong pitch is a scrapped part.
            continue
        thread["pitch_mm"] = pitch
        assumptions.append(
            Assumption(
                path=f"{body_path}.outer.{index}.thread",
                field="pitch_mm",
                value=pitch,
                rule=f"ГОСТ 8724: крупный шаг {pitch:g} мм для M{nominal:g}",
            )
        )


def _record(spec: dict, assumptions: list[Assumption]) -> None:
    """Attach the provenance, so the value and its story travel together."""
    provenance = spec.setdefault("provenance", {})
    if not isinstance(provenance, dict):
        provenance = {}
        spec["provenance"] = provenance
    for item in assumptions:
        provenance[f"{item.path}.{item.field}"] = {
            "origin": item.origin,
            "detail": item.rule,
            "value_mm": item.value,
        }
