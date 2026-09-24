"""Синтетический план этажа с известным эталоном (Ф7.3).

На 10 реальных DWG проёмов шесть и ни одной двери с дугой открывания —
замер проёмов там мерить нечем. План генерируется так же, как листы деталей:
по построению известно, где каждая стена, проём, дверь и окно.

Условности — ГОСТ 21.201/21.501: стены в разрезе — контур основной линией и
штриховка под 45°, дверь — разрыв стены, полотно и дуга открывания, окно —
разрыв стены с тонкими линиями остекления вдоль неё. Оси — штрихпунктир и
маркеры в кружках, между маркерами — размерная цепочка.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

# Шаги сетки осей, мм (кратно 300, как в строительных модулях).
_SPANS = tuple(range(3000, 7201, 300))
_OUTER = (380.0, 510.0)
_INNER = (120.0, 250.0)
_DOOR = (800.0, 900.0, 1000.0)
_WINDOW = (1200.0, 1500.0, 1800.0)


@dataclass
class PlanTruth:
    """Эталон листа: всё в пикселях растра (y вниз), толщины и ширины — px."""

    mm_per_px: float
    size: tuple[int, int]
    openings: list[dict[str, Any]] = field(default_factory=list)
    spans_mm: list[float] = field(default_factory=list)
    markers: list[tuple[float, float, float]] = field(default_factory=list)


def random_plan(seed: int, *, mm_per_px: float = 8.0) -> tuple[Any, PlanTruth]:
    """(растр в оттенках серого, эталон)."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    rng = random.Random(seed)
    xs = [0.0]
    for _ in range(rng.randint(2, 4)):
        xs.append(xs[-1] + rng.choice(_SPANS))
    ys = [0.0]
    for _ in range(rng.randint(2, 3)):
        ys.append(ys[-1] + rng.choice(_SPANS))
    outer = rng.choice(_OUTER)
    margin_mm = 4200.0
    width_px = int((xs[-1] + 2 * margin_mm) / mm_per_px)
    height_px = int((ys[-1] + 2 * margin_mm) / mm_per_px)

    def px(x_mm: float, y_mm: float) -> tuple[float, float]:
        return ((x_mm + margin_mm) / mm_per_px, (ys[-1] + margin_mm - y_mm) / mm_per_px)

    # Стены: (ось вдоль, координата осевой, от, до, толщина, наружная?).
    walls: list[tuple[str, float, float, float, float, bool]] = []
    for y in (ys[0], ys[-1]):
        walls.append(("h", y, xs[0], xs[-1], outer, True))
    for x in (xs[0], xs[-1]):
        walls.append(("v", x, ys[0], ys[-1], outer, True))
    for x in xs[1:-1]:
        if rng.random() < 0.7:
            walls.append(("v", x, ys[0], ys[-1], rng.choice(_INNER), False))
    for y in ys[1:-1]:
        if rng.random() < 0.7:
            walls.append(("h", y, xs[0], xs[-1], rng.choice(_INNER), False))

    mask = np.zeros((height_px, width_px), bool)

    def fill(axis: str, position: float, start: float, end: float, thickness: float, on: bool):
        if axis == "h":
            x0, y0 = px(start, position + thickness / 2)
            x1, y1 = px(end, position - thickness / 2)
        else:
            x0, y0 = px(position - thickness / 2, end)
            x1, y1 = px(position + thickness / 2, start)
        mask[int(round(y0)) : int(round(y1)), int(round(x0)) : int(round(x1))] = on

    for axis, position, start, end, thickness, _outer in walls:
        half = outer / 2
        fill(axis, position, start - half, end + half, thickness, True)

    # Проёмы: в каждом пролёте стены между соседними осями — с вероятностью.
    openings: list[dict[str, Any]] = []
    for axis, position, start, end, thickness, is_outer in walls:
        stops = xs if axis == "h" else ys
        stops = [value for value in stops if start <= value <= end]
        for low, high in zip(stops, stops[1:], strict=False):
            if rng.random() < 0.45:
                continue
            width = rng.choice(_WINDOW if is_outer else _DOOR)
            room = (high - low) - width - 2 * outer
            if room < 600:
                continue
            begin = low + outer + 300 + rng.random() * (room - 600)
            fill(axis, position, begin, begin + width, thickness + 2, False)
            openings.append(
                {
                    "axis": axis,
                    "position_mm": position,
                    "start_mm": begin,
                    "end_mm": begin + width,
                    "thickness_mm": thickness,
                    "kind": "window" if is_outer else "door",
                    "hinge": rng.choice(("start", "end")),
                    "side": rng.choice((-1, 1)),
                }
            )

    image = Image.new("L", (width_px, height_px), 255)
    draw = ImageDraw.Draw(image)
    # Штриховка стен под 45° — только внутри маски.
    hatch = Image.new("L", (width_px, height_px), 255)
    hatch_draw = ImageDraw.Draw(hatch)
    step = 14
    for offset in range(-height_px, width_px, step):
        hatch_draw.line([(offset, height_px), (offset + height_px, 0)], fill=0, width=1)
    hatch_array = np.asarray(hatch)
    canvas = np.asarray(image).copy()
    canvas[mask & (hatch_array < 128)] = 0
    # Контур стен — основная линия 3 px ПО границе стены, как в CAD: линия,
    # нарисованная внутрь маски, делала перегородку 120 мм «тоньше» на
    # толщину линии (88 мм — ниже порога стены).
    from scipy import ndimage

    edge = ndimage.binary_dilation(mask, iterations=1) & ~ndimage.binary_erosion(mask, iterations=2)
    canvas[edge] = 0
    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)

    for item in openings:
        axis, position, thickness = item["axis"], item["position_mm"], item["thickness_mm"]
        start, end = item["start_mm"], item["end_mm"]
        width = end - start
        if item["kind"] == "window":
            # Остекление: две тонкие линии вдоль стены в разрыве.
            for shift in (-thickness / 6, thickness / 6):
                if axis == "h":
                    a, b = px(start, position + shift), px(end, position + shift)
                else:
                    a, b = px(position + shift, start), px(position + shift, end)
                draw.line([a, b], fill=0, width=1)
            continue
        hinge = start if item["hinge"] == "start" else end
        toward = 1.0 if item["hinge"] == "start" else -1.0
        side = float(item["side"])
        face = position + side * thickness / 2
        # Полотно — перпендикуляр от петли на ширину проёма; дуга — четверть
        # окружности от конца полотна до другого края проёма.
        if axis == "h":
            leaf = [px(hinge, face), px(hinge, face + side * width)]
        else:
            leaf = [px(face, hinge), px(face + side * width, hinge)]
        draw.line(leaf, fill=0, width=2)
        import math

        points = []
        for step_index in range(0, 41):
            angle = 0.5 * math.pi * step_index / 40
            along = hinge + toward * width * math.cos(angle)
            across = face + side * width * math.sin(angle)
            points.append(px(along, across) if axis == "h" else px(across, along))
        draw.line(points, fill=0, width=1)

    # Оси, маркеры и цепочки: снизу (цифры) и слева (буквы).
    font = ImageFont.truetype("DejaVuSans.ttf", 30)
    # Маркер оси — кружок Ø 6…12 мм листа (ГОСТ 21.101): при 1:100 и 8 мм/px
    # радиус 40 px — Ø 6,4 мм. Цепочка — сразу за маркером, в двух радиусах:
    # маркеры вдвое мельче и цепочка втрое дальше в вырез звена не попадали.
    radius = 40
    marker_list: list[tuple[float, float, float]] = []
    below = ys[0] - outer / 2 - 2600
    left = xs[0] - outer / 2 - 2600
    for index, x in enumerate(xs):
        top = px(x, ys[-1] + outer / 2 + 400)
        bottom = px(x, below)
        marker_list.append((bottom[0], bottom[1], float(radius)))
        _dash_dot(draw, top, (bottom[0], bottom[1] - radius))
        draw.ellipse(
            [bottom[0] - radius, bottom[1] - radius, bottom[0] + radius, bottom[1] + radius],
            outline=0,
            width=2,
        )
        draw.text(bottom, str(index + 1), fill=0, font=font, anchor="mm")
    for index, y in enumerate(ys):
        right = px(xs[-1] + outer / 2 + 400, y)
        marker = px(left, y)
        marker_list.append((marker[0], marker[1], float(radius)))
        _dash_dot(draw, (marker[0] + radius, marker[1]), right)
        draw.ellipse(
            [marker[0] - radius, marker[1] - radius, marker[0] + radius, marker[1] + radius],
            outline=0,
            width=2,
        )
        draw.text(marker, "АБВГДЕ"[index], fill=0, font=font, anchor="mm")
    chain_y = px(0, below + 2 * radius * mm_per_px)[1]
    for a, b in zip(xs, xs[1:], strict=False):
        pa, pb = px(a, 0), px(b, 0)
        draw.line([(pa[0], chain_y), (pb[0], chain_y)], fill=0, width=1)
        for tick in (pa[0], pb[0]):
            draw.line([(tick - 6, chain_y + 6), (tick + 6, chain_y - 6)], fill=0, width=2)
        draw.text(
            ((pa[0] + pb[0]) / 2, chain_y - 6), f"{round(b - a)}", fill=0, font=font, anchor="mb"
        )
    chain_x = px(left + 2 * radius * mm_per_px, 0)[0]
    for a, b in zip(ys, ys[1:], strict=False):
        pa, pb = px(0, a), px(0, b)
        draw.line([(chain_x, pa[1]), (chain_x, pb[1])], fill=0, width=1)
        for tick in (pa[1], pb[1]):
            draw.line([(chain_x - 6, tick + 6), (chain_x + 6, tick - 6)], fill=0, width=2)
        label = Image.new("L", (120, 30), 255)
        ImageDraw.Draw(label).text((60, 28), f"{round(b - a)}", fill=0, font=font, anchor="mb")
        rotated = label.rotate(90, expand=True)
        image.paste(
            rotated,
            (int(chain_x - 30), int((pa[1] + pb[1]) / 2 - 60)),
            rotated.point(lambda value: 255 if value < 128 else 0),
        )

    truth = PlanTruth(mm_per_px=mm_per_px, size=(width_px, height_px))
    truth.spans_mm = [b - a for a, b in zip(xs, xs[1:], strict=False)]
    truth.markers = marker_list
    for item in openings:
        if item["axis"] == "h":
            position = px(0, item["position_mm"])[1]
            start, end = px(item["start_mm"], 0)[0], px(item["end_mm"], 0)[0]
        else:
            position = px(item["position_mm"], 0)[0]
            start, end = px(0, item["end_mm"])[1], px(0, item["start_mm"])[1]
        truth.openings.append(
            {
                "axis": item["axis"],
                "position": position,
                "start": min(start, end),
                "end": max(start, end),
                "thickness": item["thickness_mm"] / mm_per_px,
                "kind": item["kind"],
            }
        )
    return image, truth


def _dash_dot(draw: Any, a: tuple[float, float], b: tuple[float, float]) -> None:
    """Осевая: штрихпунктир (штрих 30 px, пропуск, точка, пропуск)."""
    import math

    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length <= 0:
        return
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    position = 0.0
    while position < length:
        end = min(position + 30.0, length)
        draw.line(
            [(a[0] + ux * position, a[1] + uy * position), (a[0] + ux * end, a[1] + uy * end)],
            fill=0,
            width=1,
        )
        dot = end + 6.0
        if dot < length:
            draw.point((a[0] + ux * dot, a[1] + uy * dot), fill=0)
        position = end + 12.0
