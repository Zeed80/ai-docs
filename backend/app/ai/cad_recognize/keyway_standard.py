"""Шпоночный паз обязан лежать на ОДНОЙ ступени и иметь её сечение.

Две вещи, которых чтение не проверяло, и обе видны оператору как «пазы не там
и не такие».

**Не там.** Паз фрезеруется в одной цилиндрической ступени. Спек этого не
требовал: проверялось лишь, что паз укладывается в общую длину детали. Живой
прогон `z4-r4.jpg` дал профиль Ø18(0-15) Ø25(15-34) Ø35(34-54) Ø30(54-78)
Ø25(78-93) Ø24,5(93-103) Ø24(103-118) Ø22(118-153) и пазы 46-68 и 78-103 —
каждый верхом на границе двух ступеней. Такой детали не существует.

**Не такие.** Глубина сравнивалась с радиусом САМОЙ ТОЛСТОЙ ступени, а не той,
в которой паз лежит: паз глубиной 4 мм на ступени Ø22 проходил проверку,
потому что где-то на валу есть Ø35. Ширина не проверялась ничем. В том же
прогоне на ступени Ø30 стояла ширина 3,5 мм, тогда как ГОСТ 23360 для этого
диаметра даёт 8 мм — число 3,5 пришло с чужой выноски.

Таблица стандарта — жёсткое, не подобранное основание: сечение шпонки
однозначно определяется диаметром вала. Поэтому расхождение с ней либо
исправляется (когда прочитанное число ничем не подтверждено), либо
показывается человеку — но никогда не проходит молча.
"""

from __future__ import annotations

from typing import Any

# ГОСТ 23360-78 (призматические шпонки), совпадает с DIN 6885:
# диаметр вала «свыше d0 до d1» → ширина b и глубина паза вала t1.
_SECTIONS: tuple[tuple[float, float, float, float], ...] = (
    (6.0, 8.0, 2.0, 1.2),
    (8.0, 10.0, 3.0, 1.8),
    (10.0, 12.0, 4.0, 2.5),
    (12.0, 17.0, 5.0, 3.0),
    (17.0, 22.0, 6.0, 3.5),
    (22.0, 30.0, 8.0, 4.0),
    (30.0, 38.0, 10.0, 5.0),
    (38.0, 44.0, 12.0, 5.0),
    (44.0, 50.0, 14.0, 5.5),
    (50.0, 58.0, 16.0, 6.0),
    (58.0, 65.0, 18.0, 7.0),
    (65.0, 75.0, 20.0, 7.5),
    (75.0, 85.0, 22.0, 9.0),
    (85.0, 95.0, 25.0, 9.0),
    (95.0, 110.0, 28.0, 10.0),
    (110.0, 130.0, 32.0, 11.0),
)

# Насколько прочитанное сечение может отличаться от табличного, оставаясь «тем
# же». Шпонки делают и нестандартными, поэтому допуск здесь — не про точность
# измерения, а про «это явно другая шпонка».
_SECTION_TOLERANCE = 0.15


def standard_section(shaft_diameter_mm: float) -> tuple[float, float] | None:
    """Ширина и глубина паза вала по ГОСТ 23360 для этого диаметра."""
    for low, high, width, depth in _SECTIONS:
        if low < shaft_diameter_mm <= high:
            return width, depth
    return None


def steps_with_stations(outer: list[dict[str, Any]]) -> list[tuple[float, float, dict]]:
    """Ступени с их осевыми границами слева направо."""
    stations: list[tuple[float, float, dict]] = []
    position = 0.0
    for section in outer:
        length = _num(section.get("length_mm"))
        if not length or length <= 0:
            continue
        stations.append((position, position + length, section))
        position += length
    return stations


def step_for(stations: list[tuple[float, float, dict]], start: float, length: float):
    """Ступень, в которой лежит паз, и лежит ли он в ней целиком."""
    if not stations:
        return None, False
    middle = start + length / 2.0
    holder = next(
        (item for item in stations if item[0] - 1e-6 <= middle <= item[1] + 1e-6),
        None,
    )
    if holder is None:
        return None, False
    contained = holder[0] - 1e-6 <= start and start + length <= holder[1] + 1e-6
    return holder, contained


def ground_keyways(body: dict[str, Any], unresolved: list[str]) -> dict[str, int]:
    """Привязать пазы к ступеням и свести их сечение с ГОСТ 23360.

    Меняются только те числа, за которыми не стоит свидетельства: прочитанное
    с подтверждением остаётся как есть и уходит человеку с пометкой. Молчаливой
    подмены не происходит ни в одном из двух случаев — обе ветки пишут в
    ``unresolved``.
    """
    summary = {"examined": 0, "straddling": 0, "corrected_values": 0, "flagged": 0}
    keyways = [item for item in (body.get("keyways") or []) if isinstance(item, dict)]
    if not keyways:
        return summary
    stations = steps_with_stations(
        [item for item in (body.get("outer") or []) if isinstance(item, dict)]
    )
    if not stations:
        return summary
    summary["examined"] = len(keyways)

    for index, keyway in enumerate(keyways):
        start = _num(keyway.get("axial_start_mm"))
        length = _num(keyway.get("length_mm"))
        if start is None or not length:
            continue
        holder, contained = step_for(stations, start, length)
        if holder is None:
            unresolved.append(
                f"шпоночный паз {index}: {start:g}..{start + length:g} мм не попадает "
                "ни на одну ступень контура"
            )
            continue
        low, high, section = holder
        diameter = _num(section.get("diameter_mm"))
        keyway["on_section_id"] = section.get("id")
        if not contained:
            summary["straddling"] += 1
            keyway["review_required"] = True
            unresolved.append(
                f"шпоночный паз {index}: {start:g}..{start + length:g} мм выходит за "
                f"ступень Ø{diameter:g} ({low:g}..{high:g} мм) — паз фрезеруется "
                "в одной ступени, положение или длина прочитаны неверно"
            )
        if not diameter:
            continue
        standard = standard_section(diameter)
        if standard is None:
            continue
        _reconcile_section(keyway, index, diameter, standard, unresolved, summary)

    return summary


def _reconcile_section(
    keyway: dict[str, Any],
    index: int,
    diameter: float,
    standard: tuple[float, float],
    unresolved: list[str],
    summary: dict[str, int],
) -> None:
    width_std, depth_std = standard
    backed = bool(keyway.get("evidence"))
    for field, expected, title in (
        ("width_mm", width_std, "ширина"),
        ("depth_mm", depth_std, "глубина"),
    ):
        value = _num(keyway.get(field))
        if value is None or value <= 0:
            continue
        if abs(value - expected) <= expected * _SECTION_TOLERANCE:
            continue
        if backed:
            summary["flagged"] += 1
            keyway["review_required"] = True
            unresolved.append(
                f"шпоночный паз {index}: {title} {value:g} мм при Ø{diameter:g}, "
                f"а ГОСТ 23360 даёт {expected:g} мм — проверьте выноску"
            )
            continue
        summary["corrected_values"] += 1
        keyway[field] = expected
        keyway["standard_ref"] = "ГОСТ 23360"
        unresolved.append(
            f"шпоночный паз {index}: {title} {value:g} мм ничем не подтверждена и "
            f"не сходится с ГОСТ 23360 для Ø{diameter:g}; принята табличная "
            f"{expected:g} мм"
        )


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
