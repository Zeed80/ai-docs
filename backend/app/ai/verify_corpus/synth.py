"""Случайные, но КОРРЕКТНЫЕ детали в формате спека оцифровки.

Отличие от `lora_synth_specs`: тот пишет свой формат (`segments[{diameter}]`)
для рендера LoRA-датасета и не знает пазов, канавок и поперечных отверстий —
как раз тех элементов, которые проверяльщикам нужнее всего. Здесь спек сразу
в форме `EngineeringDrawingSpec`, и деталь обязана быть изготовимой: эталон,
который сам нарушает ГОСТ или кладёт паз верхом на уступ, учил бы проверку
принимать брак.

Детерминирован по seed: один и тот же seed всегда даёт ту же деталь, поэтому
корпус воспроизводим и split dev/holdout не плывёт между прогонами.
"""

from __future__ import annotations

import random
from typing import Any

from app.ai.cad_recognize.keyway_standard import standard_section
from app.ai.lora_synth_specs import _MATERIALS, _NAMES_SHAFT

# Ряд нормальных диаметров (ГОСТ 6636, Ra40) — то, что конструктор ставит на вал.
_DIAMETERS = (12, 14, 16, 18, 20, 22, 25, 28, 30, 32, 35, 40, 45, 50, 55, 60, 70, 80)
_STEP_LENGTHS = (12, 15, 18, 20, 25, 30, 35, 40, 50, 60, 70, 80)


def synth_spec(kind: str, seed: int) -> dict[str, Any]:
    """Спек детали заданного типа. Детерминирован по ``seed``."""
    rng = random.Random(f"{kind}:{seed}")
    builders = {"shaft": _shaft}
    if kind not in builders:
        raise ValueError(f"генератор для типа «{kind}» ещё не написан")
    return builders[kind](rng)


def _shaft(rng: random.Random) -> dict[str, Any]:
    outer = _stepped_profile(rng)
    total = sum(item["length_mm"] for item in outer)
    stations = _stations(outer)

    body: dict[str, Any] = {
        "name": rng.choice(_NAMES_SHAFT),
        "type": "тело вращения",
        "outer": outer,
        "keyways": _keyways(rng, stations),
        "grooves": _grooves(rng, stations),
        "cross_holes": _cross_holes(rng, stations),
        "chamfers": _chamfers(rng, outer),
    }
    if rng.random() < 0.3:
        body["bore"] = _bore(rng, outer, total)

    return {
        "schema_version": 1,
        "main_view": body,
        "views": [{"kind": "front", "body_index": 0}],
        "dimensions": [],
        "annotations": [],
        "title_block": {
            "name": body["name"],
            "material": rng.choice(_MATERIALS),
        },
        "unresolved": [],
    }


def _stepped_profile(rng: random.Random) -> list[dict[str, Any]]:
    """Ступени вала: диаметры из нормального ряда, соседние — различны.

    Средние ступени чаще толще крайних — так устроен реальный вал (посадочные
    шейки по краям, буртик посередине), и так же выглядит лист, на котором
    проверяльщику предстоит работать.
    """
    count = rng.randint(3, 7)
    peak = rng.randint(1, count - 2) if count > 2 else 0
    diameters: list[int] = []
    for index in range(count):
        distance = abs(index - peak)
        base = rng.choice(_DIAMETERS[4:14])
        diameter = max(_DIAMETERS[0], base - distance * rng.choice((0, 2, 3, 5)))
        diameter = min(_DIAMETERS, key=lambda value: abs(value - diameter))
        if diameters and diameter == diameters[-1]:
            diameter = _neighbour(diameter, rng)
        diameters.append(diameter)
    return [
        {"diameter_mm": float(diameter), "length_mm": float(rng.choice(_STEP_LENGTHS))}
        for diameter in diameters
    ]


def _neighbour(diameter: int, rng: random.Random) -> int:
    index = _DIAMETERS.index(diameter)
    shift = rng.choice((-1, 1))
    return _DIAMETERS[min(len(_DIAMETERS) - 1, max(0, index + shift))] or diameter


def _stations(outer: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    """(начало, конец, диаметр) каждой ступени."""
    position = 0.0
    result = []
    for item in outer:
        result.append((position, position + item["length_mm"], item["diameter_mm"]))
        position += item["length_mm"]
    return result


def _keyways(rng: random.Random, stations) -> list[dict[str, Any]]:
    """Пазы строго ВНУТРИ одной ступени, сечение — по ГОСТ 23360.

    Эталон, где паз стоит верхом на уступе, нельзя: такую деталь не изготовить,
    а проверка, обученная на нём, принимала бы брак за норму.
    """
    keyways = []
    for start, end, diameter in stations:
        section = standard_section(diameter)
        length = end - start
        if section is None or length < 20 or rng.random() > 0.35:
            continue
        width, depth = section
        margin = max(2.0, 0.1 * length)
        shortest = max(width * 1.5, 0.4 * length)
        longest = length - 2 * margin
        # `uniform` с перевёрнутыми границами не падает, а молча возвращает
        # число между ними — так паз 21 мм оказался на ступени в 20 мм, и
        # эталон сам стал бракованной деталью. Сверка пазов это и поймала.
        if shortest > longest:
            continue
        slot = float(int(rng.uniform(shortest, longest)))
        if slot < shortest:
            continue
        offset = rng.uniform(margin, length - margin - slot)
        keyways.append(
            {
                "kind": "parallel",
                "axial_start_mm": round(start + offset, 1),
                "length_mm": float(slot),
                "width_mm": width,
                "depth_mm": depth,
                "end_type": "closed",
                "standard_ref": "ГОСТ 23360",
            }
        )
    return keyways


def _grooves(rng: random.Random, stations) -> list[dict[str, Any]]:
    """Канавки выхода инструмента — у уступов, на меньшей из соседних ступеней."""
    grooves = []
    for (_s0, e0, d0), (_s1, _e1, d1) in zip(stations, stations[1:], strict=False):
        if rng.random() > 0.3:
            continue
        smaller = min(d0, d1)
        width = rng.choice((1.6, 2.0, 3.0))
        depth = rng.choice((0.3, 0.5, 1.0))
        if depth >= smaller / 2 * 0.2:
            depth = round(smaller / 2 * 0.1, 1) or 0.3
        position = e0 - width / 2 if d0 < d1 else e0 + width / 2
        grooves.append(
            {
                "kind": "relief",
                "axial_position_mm": round(position, 2),
                "width_mm": width,
                "depth_mm": depth,
            }
        )
    return grooves


def _cross_holes(rng: random.Random, stations) -> list[dict[str, Any]]:
    holes = []
    for start, end, diameter in stations:
        length = end - start
        if length < 15 or rng.random() > 0.2:
            continue
        hole = float(rng.choice((3, 4, 5, 6, 8)))
        if hole >= diameter * 0.4:
            continue
        holes.append(
            {
                "diameter_mm": hole,
                "axial_position_mm": round(rng.uniform(start + 5, end - 5), 1),
                "angle_deg": 0.0,
                "through": True,
            }
        )
    return holes


def _chamfers(rng: random.Random, outer: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chamfers = []
    for location, section in (("left_end", outer[0]), ("right_end", outer[-1])):
        if rng.random() < 0.7:
            size = rng.choice((0.5, 1.0, 1.6, 2.0))
            if size < section["diameter_mm"] * 0.1:
                chamfers.append({"size_mm": size, "angle_deg": 45.0, "location": location})
    return chamfers


def _bore(rng: random.Random, outer, total: float) -> list[dict[str, Any]]:
    """Сквозное центральное отверстие — тоньше самой тонкой ступени."""
    thinnest = min(item["diameter_mm"] for item in outer)
    diameter = max(4.0, round(thinnest * rng.uniform(0.3, 0.5)))
    return [{"diameter_mm": float(diameter), "length_mm": float(total)}]
