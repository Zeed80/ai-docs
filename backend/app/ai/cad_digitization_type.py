"""User-selected CAD/BIM input type and fail-closed routing hints."""

from __future__ import annotations

from dataclasses import dataclass


DIGITIZATION_TYPES = (
    "auto",
    "rotation_body",
    "arbitrary_mechanical_part",
    "mechanical_assembly",
    "construction_structure",
    "architectural_drawing",
    "mep_systems",
    "electrical_scheme",
    "hydraulic_scheme",
    "pid_scheme",
)

_PROFILE_BY_TYPE = {
    "auto": "auto",
    "rotation_body": "mechanical",
    "arbitrary_mechanical_part": "mechanical",
    "mechanical_assembly": "mechanical",
    "construction_structure": "construction",
    "architectural_drawing": "construction",
    "mep_systems": "construction",
    "electrical_scheme": "electrical",
    "hydraulic_scheme": "hydraulic",
    "pid_scheme": "pid",
}

_SPEC_REDRAW_TYPES = {
    "auto",
    "rotation_body",
    "arbitrary_mechanical_part",
}


@dataclass(frozen=True)
class DigitizationTypeDecision:
    requested: str
    normalized: str
    profile: str
    explicit: bool
    spec_redraw_supported: bool


def resolve_digitization_type(value: str | None) -> DigitizationTypeDecision:
    requested = (value or "auto").strip().lower()
    normalized = requested if requested in DIGITIZATION_TYPES else "auto"
    return DigitizationTypeDecision(
        requested=requested,
        normalized=normalized,
        profile=_PROFILE_BY_TYPE[normalized],
        explicit=normalized != "auto",
        spec_redraw_supported=normalized in _SPEC_REDRAW_TYPES,
    )


def validate_spec_for_digitization_type(
    spec: dict, digitization_type: str | None
) -> list[str]:
    """Return user-actionable blockers without inventing a different class."""

    decision = resolve_digitization_type(digitization_type)
    if decision.normalized != "rotation_body":
        return []
    body = spec.get("main_view") or {}
    if body.get("outer"):
        return []
    return [
        "Выбран тип «тело вращения», но чтение не подтвердило осевой "
        "ступенчатый профиль. Построение другим типом детали запрещено."
    ]
