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

import math
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
    builders = {
        "shaft": _shaft,
        "plate": _plate,
        "flange": _flange,
        "hub_flange": _hub_flange,
        "housing": _housing,
        "sheet_metal": _sheet_metal,
        "weldment": _weldment,
    }
    if kind not in builders:
        raise ValueError(f"генератор для типа «{kind}» ещё не написан")
    return builders[kind](rng)


def _shaft(rng: random.Random) -> dict[str, Any]:
    outer = _stepped_profile(rng)
    total = sum(item["length_mm"] for item in outer)
    stations = _stations(outer)

    name = rng.choice(_NAMES_SHAFT)
    keyways = _keyways(rng, stations)
    grooves = _grooves(rng, stations)
    body: dict[str, Any] = {
        "name": name,
        "type": "тело вращения",
        "outer": outer,
        "keyways": keyways,
        "grooves": grooves,
        "cross_holes": _cross_holes(rng, stations, keyways),
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


def _cross_holes(
    rng: random.Random, stations, keyways: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Поперечные отверстия — не в пазу.

    Генератор ставил отверстие в конец паза (shaft-6: Ø6 на 94,9 в пазу
    83..96) — такой детали не делают, и контур паза на листе ломается.
    Попавшее в паз отверстие сдвигается к ближайшему свободному месту своей
    ступени — без новых случайных чисел, чтобы остальной корпус не поплыл;
    места нет — отверстия нет.
    """
    busy = [(k["axial_start_mm"], k["axial_start_mm"] + k["length_mm"]) for k in keyways or []]
    holes = []
    for start, end, diameter in stations:
        length = end - start
        if length < 15 or rng.random() > 0.2:
            continue
        hole = float(rng.choice((3, 4, 5, 6, 8)))
        if hole >= diameter * 0.4:
            continue
        position = round(rng.uniform(start + 5, end - 5), 1)
        clearance = hole / 2.0 + 1.0
        position = _free_position(position, start + 5, end - 5, busy, clearance)
        if position is None:
            continue
        holes.append(
            {
                "diameter_mm": hole,
                "axial_position_mm": position,
                "angle_deg": 0.0,
                "through": True,
            }
        )
    return holes


def _free_position(
    position: float, low: float, high: float, busy: list[tuple[float, float]], clearance: float
) -> float | None:
    """Ближайшее к ``position`` место в ``[low, high]`` не ближе ``clearance`` к занятому."""
    blocked = [(a - clearance, b + clearance) for a, b in busy]
    if not any(a < position < b for a, b in blocked):
        return position
    candidates = [low, high] + [edge for a, b in blocked for edge in (a, b)]
    free = [
        round(c, 1)
        for c in candidates
        if low <= c <= high and not any(a < c < b for a, b in blocked)
    ]
    return min(free, key=lambda c: abs(c - position)) if free else None


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


def _hub_flange(rng: random.Random) -> dict[str, Any]:
    """Фланец со ступицей — тело вращения: диск, ступица, расточка насквозь,
    отверстия на окружности через диск (X1: такой фланец не «круглая пластина»)."""
    diameter = float(rng.choice((100, 120, 140, 160, 200)))
    disk = float(rng.choice((10, 12, 15, 20)))
    hub = float(round(diameter * rng.uniform(0.4, 0.55)))
    hub_length = float(rng.choice((20, 25, 30, 40)))
    bore = float(round(hub * rng.uniform(0.4, 0.6)))
    hole = rng.choice(_HOLES)
    pcd = float(round((hub + diameter) / 2.0))
    spec = {
        "schema_version": "3.0",
        "main_view": {
            "type": "тело вращения",
            "outer": [
                {"diameter_mm": diameter, "length_mm": disk},
                {"diameter_mm": hub, "length_mm": hub_length},
            ],
            "bore": [{"diameter_mm": bore, "length_mm": disk + hub_length}],
            "circular_hole_patterns": [
                {
                    "count": rng.choice((4, 6, 8)),
                    "hole_diameter_mm": hole,
                    "bolt_circle_diameter_mm": pcd,
                    "axis_mode": "axial",
                    "start_angle_deg": 0.0,
                    "from_face": "zmin",
                    "through": True,
                }
            ],
        },
        "views": [],
        "dimensions": [],
        "annotations": [],
        "title_block": {"name": "Фланец", "material": "Сталь 20", "scale": "1:2"},
        "unresolved": [],
    }
    return spec


# Корпус (X2): коробка с полостью, приливы и карманы на стенках.
_HOUSING_WALLS = ("front", "back", "left", "right")
_BOSS_DIAMETERS = (16.0, 20.0, 25.0, 30.0)


def _housing(rng: random.Random) -> dict[str, Any]:
    """Корпус: коробка с полостью сверху, приливы и карманы на стенках.

    Полость — карман на верхней грани со стенкой и дном; приливы стоят на
    стенках снаружи (под крепёж), карманы — внутрь стенки. Всё проверяется на
    вписанность в свою грань и на непересечение с соседями: эталон, который
    сам ставит прилив за краем стенки, учил бы проверку принимать брак.
    """
    width = float(rng.choice((80, 100, 120, 150)))
    height = float(rng.choice((60, 80, 100)))
    thickness = float(rng.choice((40, 50, 60, 70)))
    wall = float(rng.choice((6, 8, 10, 12)))
    hole = float(rng.choice(_HOLES))
    # Полка вокруг полости шире стенки настолько, чтобы в ней помещалось
    # крепёжное отверстие: при полке в одну стенку Ø17,5 вылезал за край.
    rim = max(wall, hole + 8.0)
    profile: dict[str, Any] = {
        "shape": "rectangle",
        "width_mm": width,
        "height_mm": height,
        "thickness_mm": thickness,
        "holes": [],
        "hole_patterns": [],
        "slots": [],
        "wall_features": [
            {
                "kind": "pocket",
                "on_plane": "top",
                "profile": "rectangle",
                "width_mm": round(width - 2 * rim, 1),
                "height_mm": round(height - 2 * rim, 1),
                "depth_mm": round(thickness - wall, 1),
                "center_u_mm": 0.0,
                "center_v_mm": 0.0,
            }
        ],
    }
    # Грань: (размер по u, размер по v) детали.
    faces = {
        "front": (width, thickness),
        "back": (width, thickness),
        "left": (height, thickness),
        "right": (height, thickness),
    }
    taken: dict[str, list[tuple[float, float, float]]] = {name: [] for name in faces}

    def place(plane: str, reach: float) -> tuple[float, float] | None:
        face_u, face_v = faces[plane]
        margin = reach + 0.1 * min(face_u, face_v)
        if face_u <= 2 * margin or face_v <= 2 * margin:
            return None
        for _attempt in range(20):
            u = round(rng.uniform(-face_u / 2 + margin, face_u / 2 - margin), 0)
            v = round(rng.uniform(-face_v / 2 + margin, face_v / 2 - margin), 0)
            if all(
                (u - ou) ** 2 + (v - ov) ** 2 >= (reach + oreach) ** 2
                for ou, ov, oreach in taken[plane]
            ):
                taken[plane].append((u, v, reach))
                return u, v
        return None

    for plane in rng.sample(_HOUSING_WALLS, rng.randint(1, 3)):
        diameter = float(rng.choice(_BOSS_DIAMETERS))
        spot = place(plane, diameter / 2)
        if spot is None:
            continue
        profile["wall_features"].append(
            {
                "kind": "boss",
                "on_plane": plane,
                "profile": "circle",
                "diameter_mm": diameter,
                # Вылет прилива — под головку крепежа, не больше его диаметра.
                "depth_mm": float(rng.choice((6, 8, 10))),
                "center_u_mm": spot[0],
                "center_v_mm": spot[1],
            }
        )
    for plane in rng.sample(_HOUSING_WALLS, rng.randint(0, 2)):
        pocket_u = float(rng.choice((15, 20, 25)))
        pocket_v = float(rng.choice((10, 15, 20)))
        spot = place(plane, max(pocket_u, pocket_v) / 2)
        if spot is None:
            continue
        profile["wall_features"].append(
            {
                "kind": "pocket",
                "on_plane": plane,
                "profile": "rectangle",
                "width_mm": pocket_u,
                "height_mm": pocket_v,
                # Карман в стенке — не глубже самой стенки.
                "depth_mm": round(min(wall - 2.0, 6.0), 1),
                "center_u_mm": spot[0],
                "center_v_mm": spot[1],
            }
        )
    # Крепёжные отверстия по углам полки корпуса — вокруг полости.
    sx = round(width - rim, 0)
    sy = round(height - rim, 0)
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
    return _prismatic_spec(rng, profile, rng.choice(_NAMES_PLATE_RECT), "корпус")


# Гнутые профили (X4): направления гибов по порядку, +1 влево, −1 вправо.
_BENT_PROFILES = {
    "уголок": (1,),
    "швеллер": (1, 1),
    "Z-профиль": (1, -1),
    "шляпный профиль": (-1, 1, 1, -1),
}
# Толщина листа (ГОСТ 19903) и внутренний радиус гиба не меньше толщины.
_SHEET_THICKNESS = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


def _sheet_metal(rng: random.Random) -> dict[str, Any]:
    """Гнутая деталь из листа: полки и гибы на 90°, радиус, толщина, ширина.

    Полка не короче трёх толщин и радиуса гиба — короче её не согнуть; радиус
    гиба не меньше толщины. Эталон, который сам гнёт полку в 1 мм из листа 4,
    учил бы проверку принимать брак.
    """
    name, turns = rng.choice(sorted(_BENT_PROFILES.items()))
    thickness = rng.choice(_SHEET_THICKNESS)
    radius = rng.choice((1.0, 1.5, 2.0)) * thickness
    shortest = max(3.0 * thickness, radius + thickness, 8.0)
    flanges = [
        float(
            rng.choice([value for value in (10, 12, 15, 20, 25, 30, 40, 50) if value >= shortest])
        )
        for _ in range(len(turns) + 1)
    ]
    if len(flanges) == 5:
        # Шляпный профиль — стенки одной высоты, иначе полки не в одной плоскости.
        flanges[3] = flanges[1]
    # Уголок бывает и не прямым (гиб на 45…135°): у него размер полки — до
    # условной вершины, а угол проставлен отдельно.
    angles = None
    if len(turns) == 1 and rng.random() < 0.35:
        angles = [float(rng.choice((45, 60, 120, 135)))]
        # Лист ставит круглый наружный размер до условной вершины; прямой
        # участок — он минус вылет гиба (R + s)·tg(θ/2).
        reach = (radius + thickness) * math.tan(math.radians(angles[0]) / 2.0)
        flanges = [round(value - reach, 6) for value in flanges]
        if min(flanges) < shortest:
            flanges = [round(value + reach + 20.0 - reach, 6) for value in flanges]
    return {
        "schema_version": 1,
        "main_view": {
            "name": name,
            "type": "листовая деталь",
            "sheet_metal": {
                "flanges_mm": flanges,
                "turns": list(turns),
                "radius_mm": radius,
                "thickness_mm": thickness,
                "width_mm": float(rng.choice((20, 30, 40, 50, 60, 80, 100))),
                **({"bend_angles_deg": angles} if angles else {}),
            },
        },
        "views": [{"kind": "front", "body_index": 0}],
        "dimensions": [],
        "annotations": [],
        "title_block": {"name": name, "material": rng.choice(_MATERIALS)},
        "unresolved": [],
    }


def _weld_plate(width: float, height: float, thickness: float, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "type": "пластина",
        "profile": {
            "shape": "rectangle",
            "width_mm": width,
            "height_mm": height,
            "thickness_mm": thickness,
            "holes": [],
            "hole_patterns": [],
            "slots": [],
        },
    }


def _weldment(rng: random.Random) -> dict[str, Any]:
    """Сварной узел: основание и одно-два ребра на нём, угловые швы ГОСТ 5264.

    Ребро стоит на верхней грани основания вдоль его ширины; у края — шов
    с внутренней стороны (Т1, снаружи варить не на чем), в середине —
    одно- или двусторонний (Т1/Т3). Катет не больше тонкой из свариваемых
    толщин — эталон с катетом толще листа учил бы проверку принимать брак.
    """
    width = float(rng.choice((80, 100, 120, 150, 200)))
    depth = float(rng.choice((50, 60, 80, 100, 120)))
    thickness = float(rng.choice((6, 8, 10, 12)))
    parts = [_weld_plate(width, depth, thickness, "Основание")]
    welds: list[dict[str, Any]] = []
    ribs = rng.randint(1, 2)
    taken: list[tuple[float, float]] = []
    for number in range(ribs):
        rib_t = float(rng.choice((4, 5, 6, 8)))
        rib_h = float(rng.choice((30, 40, 50, 60)))
        leg = float(min(rng.choice((3, 4, 5, 6)), rib_t, thickness))
        at_edge = number == 0 and rng.random() < 0.4
        if at_edge:
            edge = depth
        else:
            edge = round(rng.uniform(rib_t + leg + 5, depth - leg - 5), 0)
        span = (edge - rib_t - leg - 5, edge + leg + 5)
        if any(not (span[1] <= lo or span[0] >= hi) for lo, hi in taken):
            continue
        taken.append(span)
        rib = _weld_plate(width, rib_h, rib_t, f"Ребро {number + 1}")
        # Поворот вокруг x на 90°: толщина ребра уходит в −y, высота — вверх.
        rib["placement"] = {
            "position_mm": [0.0, edge, thickness],
            "axis": [1.0, 0.0, 0.0],
            "angle_deg": 90.0,
        }
        parts.append(rib)
        both = not at_edge and rng.random() < 0.5
        welds.append(
            {
                "bodies": [0, len(parts) - 1],
                "designation": "Т3" if both else "Т1",
                "standard": "ГОСТ 5264-80",
                "leg_mm": leg,
                "both_sides": both,
            }
        )
    name = rng.choice(("Кронштейн", "Опора", "Стойка", "Подставка"))
    return {
        "schema_version": 1,
        "part": name,
        "main_view": {"name": name, "type": "сварной узел"},
        "parts": parts,
        "welds": welds,
        "views": [{"kind": "front", "body_index": 0}],
        "dimensions": [],
        "annotations": [],
        "title_block": {"name": name, "material": rng.choice(_MATERIALS)},
        "unresolved": [],
    }


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
