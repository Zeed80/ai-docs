"""Листовая деталь без верстака SheetMetal (план, X4, эксперимент E14).

Гнутая деталь из листа — это сечение (полки и дуги гибов толщиной t),
выдавленное на ширину. Ядро это уже умеет: эскиз основания принимает дуги, а
протяжка по пути — нет (там углы только скошенные, без радиуса гиба). Отсюда:

* :func:`bent_section` строит замкнутый эскиз сечения по полкам, направлениям
  гибов (влево/вправо на 90°), внутреннему радиусу и толщине;
* :func:`developed_length` — длина развёртки с коэффициентом нейтрального слоя
  K (ГОСТ 2.109 развёртку задаёт по нейтральному слою).

Эскиз начинается в (0, 0), как требует основание ядра; первая полка идёт вдоль
+x. Длины полок — прямые участки между гибами (без дуг).
"""

from __future__ import annotations

import math
from typing import Any


def _rotate(vector: tuple[float, float], turn: int) -> tuple[float, float]:
    """Поворот на 90° влево (turn=+1) или вправо (turn=-1)."""
    x, y = vector
    return (-y, x) if turn > 0 else (y, -x)


def bent_section(
    flanges: list[float], turns: list[int], radius: float, thickness: float
) -> list[dict[str, Any]]:
    """Замкнутый эскиз сечения гнутой детали (отрезки и дуги от (0, 0)).

    ``turns`` — по гибу между соседними полками: +1 влево, −1 вправо (90°).
    """
    if len(turns) != len(flanges) - 1:
        raise ValueError("гибов должно быть на один меньше, чем полок")
    if radius <= 0 or thickness <= 0 or any(length <= 0 for length in flanges):
        raise ValueError("радиус, толщина и полки должны быть положительны")
    half = thickness / 2.0
    middle_radius = radius + half
    # Средняя линия: отрезки и дуги по порядку.
    elements: list[tuple] = []
    point = (0.0, 0.0)
    direction = (1.0, 0.0)
    for index, length in enumerate(flanges):
        end = (point[0] + direction[0] * length, point[1] + direction[1] * length)
        elements.append(("line", point, end, direction))
        point = end
        if index < len(turns):
            turn = turns[index]
            normal = _rotate(direction, turn)
            centre = (point[0] + normal[0] * middle_radius, point[1] + normal[1] * middle_radius)
            new_direction = _rotate(direction, turn)
            end = (
                centre[0] - _rotate(new_direction, turn)[0] * middle_radius,
                centre[1] - _rotate(new_direction, turn)[1] * middle_radius,
            )
            elements.append(("arc", point, end, direction, new_direction, centre, turn))
            point = end
            direction = new_direction

    def left(direction: tuple[float, float]) -> tuple[float, float]:
        return _rotate(direction, +1)

    def offset(point: tuple[float, float], direction: tuple[float, float], side: float):
        normal = left(direction)
        return (point[0] + side * half * normal[0], point[1] + side * half * normal[1])

    # Левая сторона — вперёд, правая — назад; дуги концентричны средней.
    forward: list[dict[str, Any]] = []
    for element in elements:
        if element[0] == "line":
            _kind, _start, end, direction = element
            forward.append({"kind": "line", "to": offset(end, direction, +1.0)})
        else:
            _kind, _start, end, _d0, d1, centre, turn = element
            forward.append(
                {
                    "kind": "arc",
                    "to": offset(end, d1, +1.0),
                    "center": centre,
                    "clockwise": turn < 0,
                }
            )
    backward: list[dict[str, Any]] = []
    for element in reversed(elements):
        if element[0] == "line":
            _kind, start, _end, direction = element
            backward.append({"kind": "line", "to": offset(start, direction, -1.0)})
        else:
            _kind, start, _end, d0, _d1, centre, turn = element
            backward.append(
                {
                    "kind": "arc",
                    "to": offset(start, d0, -1.0),
                    "center": centre,
                    "clockwise": turn > 0,
                }
            )
    first = elements[0]
    start_left = offset(first[1], first[3], +1.0)
    last = elements[-1]
    end_right = offset(last[2], last[3], -1.0)
    # Контур: левая сторона вперёд, торец, правая назад, торец в начало.
    path = [*forward, {"kind": "line", "to": end_right}, *backward]
    path.append({"kind": "line", "to": start_left})
    # Эскиз ядра начинается в (0, 0): сдвиг всего контура к началу левой стороны.
    shift_x, shift_y = start_left
    sketch = []
    for segment in path:
        moved = {
            **segment,
            "to": [round(segment["to"][0] - shift_x, 6), round(segment["to"][1] - shift_y, 6)],
        }
        if "center" in segment:
            moved["center"] = [
                round(segment["center"][0] - shift_x, 6),
                round(segment["center"][1] - shift_y, 6),
            ]
        sketch.append(moved)
    return sketch


def developed_length(
    flanges: list[float], bends: int, radius: float, thickness: float, k_factor: float = 0.5
) -> float:
    """Длина развёртки: полки + дуги гибов по нейтральному слою R + K·t."""
    return float(sum(flanges)) + bends * (math.pi / 2.0) * (radius + k_factor * thickness)


def section_area(flanges: list[float], bends: int, radius: float, thickness: float) -> float:
    """Площадь сечения: полки t × L и четверть кольца на каждом гибе."""
    return thickness * float(sum(flanges)) + bends * (math.pi / 4.0) * (
        (radius + thickness) ** 2 - radius**2
    )


def flange_spans(
    flanges: list[float], turns: list[int], radius: float, thickness: float
) -> list[dict[str, Any]]:
    """Наружные размеры полок в системе эскиза :func:`bent_section`.

    На чертеже гнутой детали полку образмеривают по наружной поверхности
    (ГОСТ 2.307): от свободного торца или наружной поверхности соседней полки
    до наружной поверхности следующей. Это прямой участок плюс ``R + s`` на
    каждый прилегающий гиб. Возвращается по полке: ``start``/``end`` — концы
    размера вдоль полки на её наружной стороне, ``outward`` — нормаль наружу,
    ``value`` — значение, ``axis`` — ``"x"`` или ``"y"``.
    """
    half = thickness / 2.0
    middle_radius = radius + half
    point = (0.0, 0.0)
    direction = (1.0, 0.0)
    walked: list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]] = []
    for index, length in enumerate(flanges):
        end = (point[0] + direction[0] * length, point[1] + direction[1] * length)
        walked.append((point, end, direction))
        point = end
        if index < len(turns):
            turn = turns[index]
            normal = _rotate(direction, turn)
            centre = (point[0] + normal[0] * middle_radius, point[1] + normal[1] * middle_radius)
            new_direction = _rotate(direction, turn)
            point = (
                centre[0] - _rotate(new_direction, turn)[0] * middle_radius,
                centre[1] - _rotate(new_direction, turn)[1] * middle_radius,
            )
            direction = new_direction
    spans: list[dict[str, Any]] = []
    reach = radius + thickness
    for index, (start, end, direction) in enumerate(walked):
        before = turns[index - 1] if index > 0 else None
        after = turns[index] if index < len(turns) else None
        # Наружная сторона — против поворота гиба (у свободной полки — против
        # единственного её гиба).
        turn = after if after is not None else before
        outward = _rotate(direction, -turn if turn else -1)
        lead = reach if before is not None else 0.0
        tail = reach if after is not None else 0.0
        a = (start[0] - direction[0] * lead, start[1] - direction[1] * lead)
        b = (end[0] + direction[0] * tail, end[1] + direction[1] * tail)
        a = (a[0] + outward[0] * half, a[1] + outward[1] * half - half)
        b = (b[0] + outward[0] * half, b[1] + outward[1] * half - half)
        spans.append(
            {
                "start": [round(a[0], 6), round(a[1], 6)],
                "end": [round(b[0], 6), round(b[1], 6)],
                "outward": [round(outward[0]), round(outward[1])],
                "value": round(flanges[index] + lead + tail, 6),
                "axis": "x" if abs(direction[0]) > 0.5 else "y",
            }
        )
    return spans
