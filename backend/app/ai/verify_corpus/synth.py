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
from app.ai.lora_synth_specs import (
    _MATERIALS,
    _NAMES_PLATE_CIRCLE,
    _NAMES_PLATE_RECT,
    _NAMES_SHAFT,
)

# Ряд нормальных диаметров (ГОСТ 6636, Ra40) — то, что конструктор ставит на вал.
_DIAMETERS = (12, 14, 16, 18, 20, 22, 25, 28, 30, 32, 35, 40, 45, 50, 55, 60, 70, 80)
_STEP_LENGTHS = (12, 15, 18, 20, 25, 30, 35, 40, 50, 60, 70, 80)


def synth_spec(kind: str, seed: int) -> dict[str, Any]:
    """Спек детали заданного типа. Детерминирован по ``seed``."""
    rng = random.Random(f"{kind}:{seed}")
    builders = {"shaft": _shaft, "plate": _plate, "flange": _flange}
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
    """Соседний диаметр ряда — обязательно ДРУГОЙ.

    На краю ряда сдвиг «наружу» упирался в границу и возвращал тот же диаметр:
    у shaft-21 две соседние ступени получили Ø12. На теле это один цилиндр,
    уступа между ними нет, и «длина ступени 25 мм» у детали физически не
    существует — лист справедливо её не проставил, а метрика полноты сочла это
    пропуском. У края идём внутрь ряда.
    """
    index = _DIAMETERS.index(diameter)
    if index == 0:
        return _DIAMETERS[1]
    if index == len(_DIAMETERS) - 1:
        return _DIAMETERS[-2]
    return _DIAMETERS[index + rng.choice((-1, 1))]


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


# ── Пластины и фланцы ───────────────────────────────────────────────────────

_THICKNESSES = (5, 6, 8, 10, 12, 16, 20, 25)
_HOLES = (5.5, 6.6, 9, 11, 13.5, 17.5)  # под крепёж М5..М16, ГОСТ 11284


def _plate(rng: random.Random) -> dict[str, Any]:
    """Прямоугольная пластина: отверстия по координатам, массивы, прорези.

    Отверстия ставятся по координатам, а не только в центр: координаты
    отверстий пластины фрагментный путь продукта сейчас не читает вовсе (только
    центральное (0,0)), и эталон обязан это показывать.
    """
    width = float(rng.choice((60, 80, 100, 120, 150, 200)))
    height = float(rng.choice((40, 50, 60, 80, 100)))
    profile: dict[str, Any] = {
        "shape": "rectangle",
        "width_mm": width,
        "height_mm": height,
        "thickness_mm": float(rng.choice(_THICKNESSES)),
        "holes": [],
        "hole_patterns": [],
        "slots": [],
    }
    if rng.random() < 0.4:
        profile["corner_radius_mm"] = float(rng.choice((3, 5, 8, 10)))
    occupied: list[tuple[float, float, float]] = []  # (x, y, радиус с запасом)
    margin = 0.12 * min(width, height)

    def free(x: float, y: float, radius: float) -> bool:
        inside = (
            abs(x) + radius <= width / 2 - margin / 2 and abs(y) + radius <= height / 2 - margin / 2
        )
        clear = all(
            (x - ox) ** 2 + (y - oy) ** 2 >= (radius + orad) ** 2 for ox, oy, orad in occupied
        )
        return inside and clear

    if rng.random() < 0.5:
        # Прямоугольный массив по углам — самый частый крепёж пластины.
        hole = rng.choice(_HOLES)
        sx = round(width - 2 * margin, 0)
        sy = round(height - 2 * margin, 0)
        if sx > hole * 2 and sy > hole * 2:
            profile["hole_patterns"].append(
                {
                    "kind": "rectangular",
                    "hole_diameter_mm": hole,
                    "rows": 2,
                    "columns": 2,
                    "spacing_x_mm": sx,
                    "spacing_y_mm": sy,
                    "start_x_mm": -sx / 2,
                    "start_y_mm": -sy / 2,
                }
            )
            for cx in (-sx / 2, sx / 2):
                for cy in (-sy / 2, sy / 2):
                    occupied.append((cx, cy, hole / 2 + 2))
    for _ in range(rng.randint(0, 4)):
        hole = rng.choice(_HOLES)
        for _attempt in range(20):
            x = round(rng.uniform(-width / 2, width / 2), 0)
            y = round(rng.uniform(-height / 2, height / 2), 0)
            if free(x, y, hole / 2 + 2):
                profile["holes"].append({"center_x_mm": x, "center_y_mm": y, "diameter_mm": hole})
                occupied.append((x, y, hole / 2 + 2))
                break
    if rng.random() < 0.35:
        slot_width = float(rng.choice((6, 8, 10, 12)))
        slot_length = float(rng.choice((20, 25, 30, 40)))
        for _attempt in range(20):
            x = round(rng.uniform(-width / 4, width / 4), 0)
            y = round(rng.uniform(-height / 4, height / 4), 0)
            if free(x, y, slot_length / 2 + 2):
                profile["slots"].append(
                    {
                        "center_x_mm": x,
                        "center_y_mm": y,
                        "length_mm": slot_length,
                        "width_mm": slot_width,
                    }
                )
                occupied.append((x, y, slot_length / 2 + 2))
                break
    return _prismatic_spec(rng, profile, rng.choice(_NAMES_PLATE_RECT), "пластина")


def _flange(rng: random.Random) -> dict[str, Any]:
    """Круглый фланец: центральное отверстие и окружность болтов со СЛУЧАЙНОЙ фазой.

    У продукта фаза массива сейчас всегда 0° — эталон обязан это ловить, иначе
    проверяльщику фазы нечего проверять.
    """
    diameter = float(rng.choice((80, 100, 120, 140, 160, 200, 250)))
    bore = float(round(diameter * rng.uniform(0.2, 0.35)))
    hole = rng.choice(_HOLES)
    inner = bore / 2 + hole / 2 + 4
    outer = diameter / 2 - hole / 2 - 4
    pcd = float(round(2 * rng.uniform(inner, outer)))
    count = rng.choice((3, 4, 6, 8))
    start = float(rng.choice((0, 15, 22.5, 30, 45))) if rng.random() < 0.6 else 0.0
    profile = {
        "shape": "circle",
        "diameter_mm": diameter,
        "thickness_mm": float(rng.choice(_THICKNESSES)),
        "holes": [{"center_x_mm": 0.0, "center_y_mm": 0.0, "diameter_mm": bore}],
        "hole_patterns": [
            {
                "kind": "bolt_circle",
                "hole_diameter_mm": hole,
                "count": count,
                "bolt_circle_diameter_mm": pcd,
                "start_angle_deg": start,
            }
        ],
        "slots": [],
    }
    return _prismatic_spec(rng, profile, rng.choice(_NAMES_PLATE_CIRCLE), "фланец")


def _prismatic_spec(
    rng: random.Random, profile: dict[str, Any], name: str, kind: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "main_view": {"name": name, "type": kind, "profile": profile},
        "views": [{"kind": "front", "body_index": 0}],
        "dimensions": [],
        "annotations": [],
        "title_block": {"name": name, "material": rng.choice(_MATERIALS)},
        "unresolved": [],
    }
