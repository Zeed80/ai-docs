"""Растр листа для корпуса + эталон, записанный по тому, КАК нарисовано.

Готовые рендеры CadIR для этого не годятся: оба (`png_render`, `svg_render`)
рисуют семантический `DimensionEntity` линией между точками привязки. У
диаметра это вертикальная черта поперёк всей детали — на настоящем чертеже её
нет, а размер уже нарисован выносными, размерной линией и стрелками, которые
`dimensions_from_kernel` кладёт отдельными сущностями.

(Вторая причина, найденная тогда же, — подпись размера ставилась центром, а
рендеры читали точку как левый край, и число уезжало вправо на полширины, —
исправлена в продукте полем `TextEntity.anchor`.)

Эталон подписи — рамка, полученная от самого шрифта при рисовании, а не
вычисленная заранее: так эталон не может разойтись с растром.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from typing import Any

from app.ai.cad_ir.schema import (
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    HatchRegion,
    Polyline,
    Segment,
    TextEntity,
)

_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
# Толщины линий по ГОСТ 2.303 для сплошной основной и тонкой, мм листа.
_MAIN_MM = 0.5
_THIN_MM = 0.25


@dataclass
class RenderedSheet:
    png: bytes
    width_px: int
    height_px: int
    px_per_ir: float
    labels: list[dict[str, Any]] = field(default_factory=list)


def render_sheet(ir: CadIR, *, dpi: int, ir_px_per_mm: float) -> RenderedSheet:
    """Нарисовать лист в ``dpi`` и вернуть растр вместе с рамками подписей."""
    from PIL import Image, ImageDraw, ImageFont

    scale = (dpi / 25.4) / ir_px_per_mm
    width = max(1, int(round(ir.source.image_width * scale)))
    height = max(1, int(round(ir.source.image_height * scale)))
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    main = max(1, int(round(_MAIN_MM * dpi / 25.4)))
    thin = max(1, int(round(_THIN_MM * dpi / 25.4)))

    def xy(point) -> tuple[float, float]:
        return (float(point.x) * scale, float(point.y) * scale)

    def stroke(entity) -> int:
        return main if getattr(entity, "width_class", "main") == "main" else thin

    font_path = next((path for path in _FONTS if os.path.exists(path)), None)
    labels: list[dict[str, Any]] = []
    pending_label: dict[str, Any] | None = None

    from app.ai.cad_ir.png_render import DASH_MM, arc_points, dash_runs

    for entity in ir.entities:
        if getattr(entity, "construction", False):
            continue
        # Невидимые и осевые — штрихами по ГОСТ 2.303, как в продукте: эталон
        # рисовал их сплошными, и штрихи «невидимого отверстия» на виде
        # пластины были неотличимы от видимых кромок.
        if getattr(entity, "line_class", "") in DASH_MM and isinstance(
            entity, (Segment, Polyline, Circle, Arc)
        ):
            if isinstance(entity, Segment):
                points = [xy(entity.p1), xy(entity.p2)]
            elif isinstance(entity, Polyline):
                points = [xy(point) for point in entity.points]
                if entity.closed and points:
                    points.append(points[0])
            else:
                cx, cy = xy(entity.center)
                radius = float(entity.radius) * scale
                start, end = (
                    (0.0, 360.0)
                    if isinstance(entity, Circle)
                    else (float(entity.start_angle), float(entity.end_angle))
                )
                points = arc_points(cx, cy, radius, start, end)
            for run in dash_runs(points, entity.line_class, dpi / 25.4):
                draw.line(run, fill=0, width=stroke(entity))
            continue
        if isinstance(entity, Segment):
            draw.line([xy(entity.p1), xy(entity.p2)], fill=0, width=stroke(entity))
        elif isinstance(entity, Polyline):
            points = [xy(point) for point in entity.points]
            if entity.closed and entity.line_class == "dim":
                draw.polygon(points, fill=0)  # стрелка размера — залитая
            else:
                if entity.closed:
                    points.append(points[0])
                draw.line(points, fill=0, width=stroke(entity), joint="curve")
        elif isinstance(entity, Circle):
            cx, cy = xy(entity.center)
            # PIL кладёт обводку ВНУТРЬ рамки — середина линии оказывалась на
            # r − w/2, и на эталоне каждая окружность была меньше своего Ø на
            # толщину линии (−0,5 мм у основной). Проверяльщик отверстий честно
            # мерил середину линии и «опровергал» верные диаметры. Рамка шире
            # на полтолщины — линия лежит серединой на окружности, как у чертежа.
            w = stroke(entity)
            r = float(entity.radius) * scale + w / 2.0
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=w)
        elif isinstance(entity, Arc):
            cx, cy = xy(entity.center)
            # Обводка серединой на радиусе — см. окружность выше.
            r = float(entity.radius) * scale + stroke(entity) / 2.0
            # Соглашение ровно как у рендера продукта (`png_render`, cv2.ellipse):
            # углы передаются как есть, а при start > end cv2 меняет их местами.
            # Моя первая версия меняла знак углов — и правый конец прорези
            # рисовался вывернутой дугой, ровно на тех дугах, что идут через 0°.
            low, high = sorted((float(entity.start_angle), float(entity.end_angle)))
            draw.arc(
                [cx - r, cy - r, cx + r, cy + r],
                start=low,
                end=high,
                fill=0,
                width=stroke(entity),
            )
        elif isinstance(entity, HatchRegion):
            _hatch(draw, [xy(point) for point in entity.boundary], thin, spacing=2.0 * dpi / 25.4)
        elif isinstance(entity, TextEntity):
            if not font_path or not (entity.text or "").strip():
                continue
            size = max(6, int(round(float(entity.height) * scale)))
            font = ImageFont.truetype(font_path, size)
            cx, baseline = xy(entity.position)
            # Точка — начало базовой линии или её середина (`anchor`), как у
            # рендеров продукта. Первая версия ставила ВСЁ по центру — верно для
            # подписей размеров, неверно для штампа и надписей.
            pil_anchor = "ms" if getattr(entity, "anchor", "start") == "middle" else "ls"
            box = draw.textbbox((0, 0), entity.text, font=font, anchor=pil_anchor)
            text_width = box[2] - box[0]
            if abs(float(entity.rotation)) < 1.0:
                draw.text((cx, baseline), entity.text, fill=0, font=font, anchor=pil_anchor)
                drawn = draw.textbbox((cx, baseline), entity.text, font=font, anchor=pil_anchor)
            else:
                drawn = _draw_rotated(
                    image, entity.text, font, (cx, baseline), pil_anchor, float(entity.rotation)
                )
            pending_label = {
                "text": entity.text,
                "bbox_px": [round(value, 1) for value in drawn],
                "height_px": size,
                "width_px": text_width,
                "rotation": float(entity.rotation),
            }
            labels.append({"kind": "text", **pending_label})
        elif isinstance(entity, DimensionEntity):
            # Не рисуется: это семантика, штрихи уже нарисованы выше. Но эталон
            # записывается — и связывается с подписью, стоящей прямо перед ним.
            record = {
                "kind": "dimension",
                "dimension_kind": entity.kind,
                "value_mm": entity.value_mm,
                "text": entity.text,
                "anchors_px": [list(xy(entity.p1)), list(xy(entity.p2))],
                "label": pending_label,
            }
            labels.append(record)
            pending_label = None

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return RenderedSheet(
        png=buffer.getvalue(),
        width_px=width,
        height_px=height,
        px_per_ir=scale,
        labels=labels,
    )


def _hatch(draw, boundary: list[tuple[float, float]], width: int, *, spacing: float) -> None:
    """Штриховка под 45° внутри контура (ГОСТ 2.306, металл)."""
    if len(boundary) < 3:
        return
    from PIL import Image, ImageDraw

    xs = [point[0] for point in boundary]
    ys = [point[1] for point in boundary]
    x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1
    mask = Image.new("1", (x1 - x0 + 1, y1 - y0 + 1), 0)
    ImageDraw.Draw(mask).polygon([(x - x0, y - y0) for x, y in boundary], fill=1)
    lines = Image.new("1", mask.size, 0)
    line_draw = ImageDraw.Draw(lines)
    extent = mask.size[0] + mask.size[1]
    offset = 0.0
    while offset < extent:
        line_draw.line([(offset, 0), (offset - mask.size[1], mask.size[1])], fill=1, width=width)
        offset += spacing
    from PIL import ImageChops

    pattern = ImageChops.logical_and(mask, lines)
    target = draw._image  # noqa: SLF001 — PIL не даёт иного доступа к холсту
    black = Image.new("L", pattern.size, 0)
    target.paste(black, (x0, y0), pattern)


def _draw_rotated(image, text: str, font, point, pil_anchor: str, rotation: float) -> list[float]:
    """Повёрнутая подпись вокруг её точки привязки; рамка — по чернилам.

    Угол — по часовой (как в IR), PIL поворачивает против, отсюда минус.
    """
    from PIL import Image, ImageDraw, ImageOps

    probe = ImageDraw.Draw(image).textbbox((0, 0), text, font=font, anchor=pil_anchor)
    side = 2 * int(max(probe[2] - probe[0], probe[3] - probe[1])) + 8
    centre = side // 2
    tile = Image.new("L", (side, side), 255)
    ImageDraw.Draw(tile).text((centre, centre), text, fill=0, font=font, anchor=pil_anchor)
    tile = tile.rotate(-rotation, center=(centre, centre), fillcolor=255)
    left = int(round(point[0])) - centre
    top = int(round(point[1])) - centre
    mask = ImageOps.invert(tile)
    image.paste(tile, (left, top), mask)
    box = mask.getbbox() or (centre, centre, centre, centre)
    return [left + box[0], top + box[1], left + box[2], top + box[3]]
