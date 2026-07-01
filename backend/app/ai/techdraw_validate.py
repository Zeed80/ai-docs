"""Deterministic post-validation for LLM-generated techdraw specs.

The LLM is good at producing plausible-looking JSON, not at guaranteeing it is
engineering-correct (a Ra value outside the standard series, a tolerance letter
that doesn't apply to the given diameter, a bore wider than the shaft it's
supposedly inside). This module checks the spec AFTER generation and BEFORE
rendering — by the same "rule table + deterministic check" pattern already
used in ``normcontrol_agent.py``, just applied to a Pydantic object in memory
instead of a DB-backed process plan.

Malformed structure (wrong types, missing required fields) is NOT this
module's job — that already fails loudly as a ``pydantic.ValidationError``
when the caller constructs ``ShaftSpec(**spec)`` etc. This module only catches
values that are individually well-typed but engineering-nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.ai import techdraw_reference as tdref
from app.ai.techdraw import AssemblySpec, PlateSpec, ShaftSpec


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: Literal["error", "warning"]
    message: str
    field_path: str


def _check_shaft(s: ShaftSpec, prefix: str = "") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for i, seg in enumerate(s.segments):
        path = f"{prefix}segments[{i}]"
        if seg.roughness is not None and seg.roughness not in tdref.STANDARD_RA_SERIES:
            issues.append(ValidationIssue(
                "RA_INVALID", "error",
                f"Ra={seg.roughness:g} не входит в стандартный ряд ГОСТ 2789-73 "
                f"(ближайшее: {tdref.nearest_ra(seg.roughness):g})",
                f"{path}.roughness",
            ))
        if seg.tolerance:
            if not tdref.is_valid_tolerance_symbol(seg.tolerance):
                issues.append(ValidationIssue(
                    "TOLERANCE_UNKNOWN", "error",
                    f"Допуск '{seg.tolerance}' не распознан (ожидается напр. h6, H7, js6, k6)",
                    f"{path}.tolerance",
                ))
            elif tdref.tolerance_band(seg.tolerance, seg.diameter) is None:
                issues.append(ValidationIssue(
                    "TOLERANCE_OUT_OF_RANGE", "error",
                    f"Допуск '{seg.tolerance}' не применим к Ø{seg.diameter:g} "
                    "(вне табличного диапазона 1..400мм)",
                    f"{path}.tolerance",
                ))
        if seg.thread and tdref.parse_thread(seg.thread) is None:
            issues.append(ValidationIssue(
                "THREAD_UNKNOWN", "error",
                f"Резьба '{seg.thread}' не найдена в таблице ГОСТ 8724-2002",
                f"{path}.thread",
            ))
        if seg.bore_diameter and seg.bore_diameter >= seg.diameter:
            issues.append(ValidationIssue(
                "BORE_TOO_LARGE", "error",
                f"Внутренняя расточка Ø{seg.bore_diameter:g} ≥ наружного диаметра Ø{seg.diameter:g}",
                f"{path}.bore_diameter",
            ))
        if seg.chamfer and seg.chamfer * 2 >= seg.diameter:
            issues.append(ValidationIssue(
                "CHAMFER_TOO_LARGE", "warning",
                f"Фаска {seg.chamfer:g} слишком велика для Ø{seg.diameter:g}",
                f"{path}.chamfer",
            ))
    return issues


def _check_plate(s: PlateSpec, prefix: str = "") -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    extent = s.diameter if s.shape == "circle" else min(s.width, s.height)
    for i, hole in enumerate(s.holes):
        path = f"{prefix}holes[{i}]"
        if hole.diameter >= extent:
            issues.append(ValidationIssue(
                "HOLE_TOO_LARGE", "error",
                f"Отверстие Ø{hole.diameter:g} не помещается в деталь (габарит {extent:g})",
                f"{path}.diameter",
            ))
        if hole.tolerance and not tdref.is_valid_tolerance_symbol(hole.tolerance):
            issues.append(ValidationIssue(
                "TOLERANCE_UNKNOWN", "error",
                f"Допуск отверстия '{hole.tolerance}' не распознан",
                f"{path}.tolerance",
            ))
    if s.bolt_circle_d > 0:
        if s.bolt_circle_d >= extent:
            issues.append(ValidationIssue(
                "BOLT_CIRCLE_TOO_LARGE", "error",
                f"Делительная окружность Ø{s.bolt_circle_d:g} не помещается в деталь "
                f"(габарит {extent:g})",
                f"{prefix}bolt_circle_d",
            ))
        if s.bolt_hole_d and s.bolt_hole_d >= s.bolt_circle_d:
            issues.append(ValidationIssue(
                "BOLT_HOLE_TOO_LARGE", "error",
                f"Отверстие под болт Ø{s.bolt_hole_d:g} ≥ делительной окружности Ø{s.bolt_circle_d:g}",
                f"{prefix}bolt_hole_d",
            ))
    if s.roughness is not None and s.roughness not in tdref.STANDARD_RA_SERIES:
        issues.append(ValidationIssue(
            "RA_INVALID", "error",
            f"Ra={s.roughness:g} не входит в стандартный ряд ГОСТ 2789-73 "
            f"(ближайшее: {tdref.nearest_ra(s.roughness):g})",
            f"{prefix}roughness",
        ))
    return issues


def validate_spec(spec: dict) -> list[ValidationIssue]:
    """Deterministic engineering checks, run BEFORE rendering. Never renders.

    Raises ``pydantic.ValidationError`` if the spec's structure itself is
    malformed (wrong type/missing field) — that's a distinct failure mode from
    "well-formed but engineering-nonsense", which is what this function reports.
    """
    kind = spec.get("type")
    if kind == "shaft":
        return _check_shaft(ShaftSpec(**spec))
    if kind == "plate":
        return _check_plate(PlateSpec(**spec))
    if kind == "assembly":
        a = AssemblySpec(**spec)
        issues: list[ValidationIssue] = []
        for i, comp in enumerate(a.components):
            prefix = f"components[{i}]."
            ckind = comp.spec.get("type")
            if ckind == "shaft":
                issues.extend(_check_shaft(ShaftSpec(**comp.spec), prefix))
            elif ckind == "plate":
                issues.extend(_check_plate(PlateSpec(**comp.spec), prefix))
            else:
                issues.append(ValidationIssue(
                    "UNKNOWN_TYPE", "error",
                    f"Компонент сборки имеет неизвестный тип: {ckind!r}",
                    f"{prefix}spec.type",
                ))
        return issues
    return [ValidationIssue("UNKNOWN_TYPE", "error", f"Неизвестный тип '{kind}'", "type")]


def blocking(issues: list[ValidationIssue]) -> list[ValidationIssue]:
    return [i for i in issues if i.severity == "error"]
