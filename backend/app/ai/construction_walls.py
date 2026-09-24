"""Стены на плане этажа — замером по листу (план, Ф7.2).

Ридер плана просит у модели координаты стен в миллиметрах, и живой лист «План
на отм. 0.000» дал 4 стены из десятков, ни одной с толщиной. Но стена на плане
— это пара параллельных линий: её осевая, длина и толщина измеримы ровно так
же, как размерная линия у подписи. Читать у модели остаётся то, что написано
словами (материал, «кирпич 380»), а не то, что нарисовано.

Правило одно и для эталона, и для замера: две параллельные линии одного
направления, идущие рядом на расстоянии толщины стены и перекрывающиеся вдоль
себя. Эталон берёт эти пары из точной геометрии DXF, замер — из растра
(`find_walls`), и харнесс сравнивает одно с другим.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Толщина стены на плане этажа: перегородка в полкирпича — 120 мм, наружная
# кирпичная — до 640 мм. Ниже — линии штриховки и выноски, выше — уже не стена,
# а две разные стены рядом.
MIN_THICKNESS_MM = 90.0
MAX_THICKNESS_MM = 700.0
# Короткий кусок стены на плане этажа — это уже простенок между проёмами;
# короче полуметра пара параллельных линий чаще оказывается размерной цепочкой.
MIN_LENGTH_MM = 500.0
# Насколько линии должны перекрываться вдоль себя, чтобы быть гранями одной стены.
_OVERLAP_SHARE = 0.5


@dataclass(frozen=True)
class WallSegment:
    """Стена как её видно на плане: осевая (вдоль оси), толщина, длина.

    ``axis`` — «h» (вдоль x) или «v» (вдоль y); ``position`` — координата
    осевой поперёк стены; ``start``/``end`` — её концы вдоль стены. Единицы —
    те же, в которых пришли линии (пиксели растра или единицы чертежа).
    """

    axis: str
    position: float
    start: float
    end: float
    thickness: float

    @property
    def length(self) -> float:
        return self.end - self.start


def wall_pairs(
    lines: list[tuple[str, float, float, float]],
    *,
    min_thickness: float,
    max_thickness: float,
    min_length: float,
) -> list[WallSegment]:
    """Пары параллельных линий → стены. ``lines``: (ось, положение, от, до).

    Одна линия может войти в несколько пар (стена, к которой примыкает
    перегородка), поэтому пары не «съедают» линии; зато совпадающие стены
    (одна и та же пара, найденная дважды) сливаются.
    """
    found: list[WallSegment] = []
    for axis in ("h", "v"):
        same = [line for line in lines if line[0] == axis]
        for index, (_a, position_a, start_a, end_a) in enumerate(same):
            for _b, position_b, start_b, end_b in same[index + 1 :]:
                thickness = abs(position_a - position_b)
                if not min_thickness <= thickness <= max_thickness:
                    continue
                start, end = max(start_a, start_b), min(end_a, end_b)
                overlap = end - start
                shorter = min(end_a - start_a, end_b - start_b)
                if overlap < min_length or overlap < _OVERLAP_SHARE * shorter:
                    continue
                found.append(
                    WallSegment(
                        axis=axis,
                        position=(position_a + position_b) / 2.0,
                        start=start,
                        end=end,
                        thickness=thickness,
                    )
                )
    return _merge(found)


def _merge(walls: list[WallSegment]) -> list[WallSegment]:
    """Слить стены, которые на листе — одна и та же (те же грани, тот же пролёт)."""
    merged: list[WallSegment] = []
    for wall in sorted(walls, key=lambda item: -(item.end - item.start)):
        twin = next(
            (
                kept
                for kept in merged
                if kept.axis == wall.axis
                and abs(kept.position - wall.position) <= 0.5 * kept.thickness
                and abs(kept.thickness - wall.thickness) <= 0.35 * kept.thickness
                and min(kept.end, wall.end) - max(kept.start, wall.start)
                >= 0.5 * (wall.end - wall.start)
            ),
            None,
        )
        if twin is None:
            merged.append(wall)
    return merged


def sheet_lines(gray: Any, *, min_length_px: float) -> list[tuple[str, float, float, float]]:
    """Прямые линии листа вдоль осей: (ось, положение, от, до), px."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink, _lines

    ink = _ink(np.asarray(gray))
    result: list[tuple[str, float, float, float]] = []
    for axis, name in ((0, "h"), (1, "v")):
        for line in _lines(ink, max(4, int(round(min_length_px))), axis=axis):
            result.append((name, float(line.position), float(line.start), float(line.end)))
    return result


def find_walls(gray: Any, mm_per_px: float) -> list[WallSegment]:
    """Стены плана по растру: пары граней в миллиметрах чертежа.

    ``mm_per_px`` — масштаб плана (Ф7.1): без него «толщина 27 px» ничего не
    значит — это и перегородка при одном масштабе, и наружная стена при другом.
    """
    if not mm_per_px or mm_per_px <= 0:
        return []
    lines = sheet_lines(gray, min_length_px=MIN_LENGTH_MM / mm_per_px)
    return wall_pairs(
        lines,
        min_thickness=MIN_THICKNESS_MM / mm_per_px,
        max_thickness=MAX_THICKNESS_MM / mm_per_px,
        min_length=MIN_LENGTH_MM / mm_per_px,
    )


def walls_as_read(
    walls: list[WallSegment], *, mm_per_px: float, origin_px: tuple[float, float]
) -> list[dict[str, Any]]:
    """Измеренные стены в системе координат листа, в миллиметрах.

    Начало — левый нижний маркер оси: он и есть та «координатная сетка», в
    которой ридер должен был называть стены. Ось y листа идёт вниз, у здания —
    вверх.
    """
    read: list[dict[str, Any]] = []
    for index, wall in enumerate(walls, start=1):
        thickness = round(wall.thickness * mm_per_px, 1)
        if wall.axis == "h":
            y = round((origin_px[1] - wall.position) * mm_per_px, 1)
            start_x = round((wall.start - origin_px[0]) * mm_per_px, 1)
            end_x = round((wall.end - origin_px[0]) * mm_per_px, 1)
            entry = {"start_x_mm": start_x, "start_y_mm": y, "end_x_mm": end_x, "end_y_mm": y}
        else:
            x = round((wall.position - origin_px[0]) * mm_per_px, 1)
            start_y = round((origin_px[1] - wall.end) * mm_per_px, 1)
            end_y = round((origin_px[1] - wall.start) * mm_per_px, 1)
            entry = {"start_x_mm": x, "start_y_mm": start_y, "end_x_mm": x, "end_y_mm": end_y}
        read.append(
            {
                "id": f"wall-{index}",
                "name": f"стена по листу {index}",
                **entry,
                "thickness_mm": thickness,
                "length_mm": round(wall.length * mm_per_px, 1),
                "bbox_px": _bbox_px(wall),
            }
        )
    return read


def _bbox_px(wall: WallSegment) -> list[float]:
    """Место стены на листе — для выреза в панели (Ф9)."""
    half = wall.thickness / 2.0
    if wall.axis == "h":
        return [
            round(wall.start, 1),
            round(wall.position - half, 1),
            round(wall.end, 1),
            round(wall.position + half, 1),
        ]
    return [
        round(wall.position - half, 1),
        round(wall.start, 1),
        round(wall.position + half, 1),
        round(wall.end, 1),
    ]


async def measure_plan(image_bytes: bytes, *, ask: Any = None) -> dict[str, Any]:
    """Масштаб плана и стены по листу: то, что раньше просили у модели.

    Возвращает ``{"mm_per_px", "origin_px", "walls", "markers", "reason"}``.
    Без масштаба стен нет: «толщина 27 px» сама по себе ничего не значит.
    """
    import io

    import numpy as np
    from PIL import Image

    from app.ai.construction_axes import read_sheet_scale

    scale = await read_sheet_scale(image_bytes, ask=ask)
    if not scale.get("mm_per_px"):
        return {**scale, "walls": [], "origin_px": None}
    Image.MAX_IMAGE_PIXELS = None
    sheet = Image.open(io.BytesIO(image_bytes)).convert("L")
    markers = scale["markers"]
    origin = (min(item[0] for item in markers), max(item[1] for item in markers))
    mm_per_px = float(scale["mm_per_px"])
    gray = np.asarray(sheet)
    walls = find_walls(gray, mm_per_px)
    openings = find_openings(gray, walls, mm_per_px)
    # Остекление окна замер стен принял за тонкую стену — это не стена.
    glazing = [item["glazing"] for item in openings if item["glazing"] is not None]
    walls = [wall for wall in walls if wall not in glazing]
    return {
        **scale,
        "origin_px": [round(value, 1) for value in origin],
        "walls": walls_as_read(walls, mm_per_px=mm_per_px, origin_px=origin),
        "openings": [
            opening_as_read(item, mm_per_px=mm_per_px, origin_px=origin) for item in openings
        ],
    }


def opening_as_read(
    item: dict[str, Any], *, mm_per_px: float, origin_px: tuple[float, float]
) -> dict[str, Any]:
    """Проём в координатах плана: вид, ширина, середина, толщина стены.

    Высоты проёма на плане нет (она в экспликации или на разрезе), поэтому
    в модель здания проём не встраивается — только в отчёт и вердикты.
    """
    gap: OpeningGap = item["gap"]
    middle = (gap.start + gap.end) / 2.0
    x_px, y_px = (middle, gap.position) if gap.axis == "h" else (gap.position, middle)
    return {
        "kind": item["kind"],
        "axis": gap.axis,
        "width_mm": round(gap.width * mm_per_px, 1),
        "wall_thickness_mm": round(gap.thickness * mm_per_px, 1),
        "center_x_mm": round((x_px - origin_px[0]) * mm_per_px, 1),
        "center_y_mm": round((origin_px[1] - y_px) * mm_per_px, 1),
        **({"door": item["door"]} if item.get("door") else {}),
        "bbox_px": _bbox_px(WallSegment(gap.axis, gap.position, gap.start, gap.end, gap.thickness)),
    }


# Согласие замера с чтением: толщина и длина стены — в долях прочитанного.
_THICKNESS_SHARE = 0.25
_LENGTH_SHARE = 0.2


def _read_axis(wall: Any) -> tuple[str, float]:
    """Направление прочитанной стены и её длина, мм."""
    import math

    dx = float(wall.end_x_mm) - float(wall.start_x_mm)
    dy = float(wall.end_y_mm) - float(wall.start_y_mm)
    return ("h" if abs(dx) >= abs(dy) else "v"), math.hypot(dx, dy)


def reconcile_walls(
    read_walls: list[Any], measured: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Прочитанные стены против измеренных: (стены для модели, вердикты).

    Геометрия — замером, надписи — чтением: совпавшая стена берёт с листа
    осевую, длину и толщину, а имя, материал и «несущая» остаются от модели.
    Прочитанная стена без пары на листе НЕ опровергается (замер мог её не
    найти) — это «не измеримо» с причиной; найденная замером, которой в
    чтении не было, добавляется, как паз, найденный по листу (E19).
    """
    pool = list(measured)
    walls: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    for index, wall in enumerate(read_walls):
        axis, length = _read_axis(wall)
        thickness = getattr(wall, "thickness_mm", None)
        match = None
        for candidate in pool:
            if _measured_axis(candidate) != axis:
                continue
            if abs(candidate["length_mm"] - length) > max(_LENGTH_SHARE * length, 100.0):
                continue
            if thickness and abs(candidate["thickness_mm"] - thickness) > max(
                _THICKNESS_SHARE * thickness, 20.0
            ):
                continue
            match = candidate
            break
        item = {
            "kind": "construction_wall",
            "path": f"walls[{index}]",
            "read": {"thickness_mm": thickness, "length_mm": round(length, 1)},
        }
        if match is None:
            verdicts.append(
                {
                    **item,
                    "status": "unmeasurable",
                    "measured": {},
                    "reason": "стены такой длины и толщины замер на листе не нашёл",
                }
            )
            walls.append(_as_wall(wall, None))
            continue
        pool.remove(match)
        verdicts.append(
            {
                **item,
                "status": "confirmed",
                "measured": {
                    "thickness_mm": match["thickness_mm"],
                    "length_mm": match["length_mm"],
                },
                "evidence_bbox_px": match["bbox_px"],
                "reason": "стена измерена по листу"
                if thickness
                else "толщина стены измерена по листу (на листе не прочитана)",
            }
        )
        walls.append(_as_wall(wall, match))
    for offset, extra in enumerate(pool):
        walls.append({**{key: value for key, value in extra.items() if key != "bbox_px"}})
        verdicts.append(
            {
                "kind": "construction_wall_found",
                "path": f"walls[{len(read_walls) + offset}]",
                "status": "confirmed",
                "read": {},
                "measured": {
                    "thickness_mm": extra["thickness_mm"],
                    "length_mm": extra["length_mm"],
                },
                "evidence_bbox_px": extra["bbox_px"],
                "reason": "стена найдена замером по листу (в чтении её не было)",
            }
        )
    return walls, verdicts


def _measured_axis(wall: dict[str, Any]) -> str:
    return (
        "h"
        if abs(wall["end_x_mm"] - wall["start_x_mm"]) >= abs(wall["end_y_mm"] - wall["start_y_mm"])
        else "v"
    )


def _as_wall(read: Any, measured: dict[str, Any] | None) -> dict[str, Any]:
    """Стена для модели: место и толщина — с листа, надписи — из чтения."""
    base = {
        "id": read.id,
        "name": read.name,
        "start_x_mm": read.start_x_mm,
        "start_y_mm": read.start_y_mm,
        "end_x_mm": read.end_x_mm,
        "end_y_mm": read.end_y_mm,
        "thickness_mm": read.thickness_mm,
        "height_mm": read.height_mm,
        "load_bearing": read.load_bearing,
        "material": read.material,
    }
    if measured is None:
        return base
    return {
        **base,
        "start_x_mm": measured["start_x_mm"],
        "start_y_mm": measured["start_y_mm"],
        "end_x_mm": measured["end_x_mm"],
        "end_y_mm": measured["end_y_mm"],
        "thickness_mm": measured["thickness_mm"],
    }


# Проём на плане — разрыв стены: две соосные части одной толщины и между ними
# пролёт двери или проёма (ГОСТ 21.501: двери 600…2400 мм, ворота шире).
MIN_OPENING_MM = 500.0
MAX_OPENING_MM = 3000.0
# Дуга открывания двери — четверть окружности радиусом в ширину полотна:
# доля её точек, покрытая чернилами.
_ARC_COVERAGE = 0.6


@dataclass(frozen=True)
class OpeningGap:
    """Разрыв стены: ``start``/``end`` — края проёма вдоль стены."""

    axis: str
    position: float
    start: float
    end: float
    thickness: float

    @property
    def width(self) -> float:
        return self.end - self.start


def wall_gaps(walls: list[WallSegment], *, min_gap: float, max_gap: float) -> list[OpeningGap]:
    """Разрывы между соосными частями одной стены.

    Части — одна стена, если их осевые совпадают в пределах трети толщины и
    толщины равны с точностью до четверти; проём — между соседними частями,
    когда пролёт между ними ничем не закрыт.
    """
    gaps: list[OpeningGap] = []
    for axis in ("h", "v"):
        same = sorted((wall for wall in walls if wall.axis == axis), key=lambda item: item.start)
        for index, first in enumerate(same):
            following = [
                other
                for other in same[index + 1 :]
                if abs(other.position - first.position) <= first.thickness / 3.0
                and abs(other.thickness - first.thickness) <= 0.25 * first.thickness
                and other.start > first.end
            ]
            if not following:
                continue
            second = min(following, key=lambda item: item.start)
            width = second.start - first.end
            if not min_gap <= width <= max_gap:
                continue
            # Закрыт разрыв только стеной той же толщины; тонкая пара внутри
            # — остекление окна (ГОСТ 21.201), это и есть проём.
            # Тонкие пары (остекление двух окон одной стены) стыкуются друг с
            # другом через глухой простенок: разрыв закрыт и более толстой
            # стеной, в полосе которой он лежит.
            covered = any(
                abs(other.position - first.position)
                <= max(first.thickness / 3.0, other.thickness / 2.0)
                and other.thickness >= 0.75 * first.thickness
                and other.start < second.start
                and other.end > first.end + 0.5 * width
                and other is not first
                for other in same
            )
            if covered:
                continue
            gaps.append(
                OpeningGap(
                    axis=axis,
                    position=(first.position + second.position) / 2.0,
                    start=first.end,
                    end=second.start,
                    thickness=(first.thickness + second.thickness) / 2.0,
                )
            )
    return gaps


def door_swing(ink: Any, gap: OpeningGap) -> dict[str, Any] | None:
    """Дуга открывания у проёма: четверть окружности с центром у одного края
    проёма (на грани стены) и радиусом в его ширину. Найдена — это дверь."""
    import math

    radius = gap.width
    best: dict[str, Any] | None = None
    for hinge in (gap.start, gap.end):
        for side in (-1.0, 1.0):
            face = gap.position + side * gap.thickness / 2.0
            toward = 1.0 if hinge == gap.start else -1.0
            hits = total = 0
            for step in range(1, 24):
                angle = 0.5 * math.pi * step / 24
                along = hinge + toward * radius * math.cos(angle)
                across = face + side * radius * math.sin(angle)
                x, y = (along, across) if gap.axis == "h" else (across, along)
                column, row = int(round(x)), int(round(y))
                if not (0 <= row < ink.shape[0] and 0 <= column < ink.shape[1]):
                    continue
                total += 1
                hits += bool(ink[max(0, row - 2) : row + 3, max(0, column - 2) : column + 3].any())
            if total >= 12 and hits / total >= _ARC_COVERAGE:
                coverage = hits / total
                if best is None or coverage > best["coverage"]:
                    best = {
                        "hinge": "start" if hinge == gap.start else "end",
                        "side": int(side),
                        "coverage": round(coverage, 2),
                    }
    return best


def find_openings(gray: Any, walls: list[WallSegment], mm_per_px: float) -> list[dict[str, Any]]:
    """Проёмы плана по растру: разрыв стены и, если есть, дуга двери."""
    import numpy as np

    from app.ai.cad_recognize.verifiers.plate_frame import _ink

    ink = _ink(np.asarray(gray))
    found = []
    for gap in wall_gaps(
        walls, min_gap=MIN_OPENING_MM / mm_per_px, max_gap=MAX_OPENING_MM / mm_per_px
    ):
        glazing = _glazing(walls, gap)
        door = None if glazing else door_swing(ink, gap)
        found.append(
            {
                "gap": gap,
                "door": door,
                "glazing": glazing,
                "kind": "window" if glazing else ("door" if door else "opening"),
            }
        )
    return found


def _glazing(walls: list[WallSegment], gap: OpeningGap) -> WallSegment | None:
    """Остекление окна: тонкая пара линий вдоль стены внутри разрыва.

    Замер стен принимает её за стену толщиной в треть настоящей — у окна
    это не стена, а признак проёма; вызывающий убирает её из стен.
    """
    for wall in walls:
        if wall.axis != gap.axis or wall.thickness >= 0.75 * gap.thickness:
            continue
        if abs(wall.position - gap.position) > gap.thickness / 2.0:
            continue
        inside = min(wall.end, gap.end) - max(wall.start, gap.start)
        if inside >= 0.7 * gap.width and wall.length <= 1.3 * gap.width:
            return wall
    return None
