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


def _read_failure_reasons(spec: dict, limit: int = 2) -> list[str]:
    """Почему чтение не дало геометрии — словами самого чтения."""
    reasons: list[str] = []
    for field in ("geometry_validation_errors", "unresolved"):
        for item in spec.get(field) or []:
            text = str(item).strip()
            if text and text not in reasons:
                reasons.append(text)
            if len(reasons) >= limit:
                return reasons
    return reasons


def validate_spec_for_digitization_type(spec: dict, digitization_type: str | None) -> list[str]:
    """Return user-actionable blockers without inventing a different class.

    Раньше здесь была одна формулировка на два разных случая, и в живом прогоне
    она указала не туда. Проверка — ровно «пуст ли main_view.outer», а
    сообщение говорило, что чтение не подтвердило ТИП детали. На листе, где
    модель трижды правильно прочитала ступенчатый вал, а профиль потерялся уже
    после чтения, оператор получил обвинение в неверно выбранном типе.

    Поэтому случая теперь два: «геометрия есть, но не осевая» — это
    действительно про тип; «геометрии нет вовсе» — это про чтение, и тогда
    сообщение обязано назвать его причину.
    """

    decision = resolve_digitization_type(digitization_type)
    if decision.normalized != "rotation_body":
        return []
    body = spec.get("main_view") or {}
    if body.get("outer"):
        return []

    has_other_geometry = bool((body.get("profile") or {}).get("shape") or body.get("bore"))
    if has_other_geometry:
        return [
            "Выбран тип «тело вращения», но чтение не подтвердило осевой "
            "ступенчатый профиль. Построение другим типом детали запрещено."
        ]

    reasons = _read_failure_reasons(spec)
    detail = f" Причины: {'; '.join(reasons)}." if reasons else ""
    return [
        "Чтение не дало геометрии, поэтому тип детали проверить не на чем."
        f"{detail} Тип «тело вращения» здесь ни при чём — смотрите журнал чтения."
    ]
