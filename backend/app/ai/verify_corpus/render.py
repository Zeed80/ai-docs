"""Растр листа для корпуса + эталон, записанный по тому, КАК нарисовано.

Готовые рендеры CadIR для этого не годятся, и по двум причинам, найденным при
сборке генератора:

* оба (`png_render`, `svg_render`) рисуют семантический `DimensionEntity`
  линией между точками привязки. У диаметра это вертикальная черта поперёк
  всей детали — на настоящем чертеже её нет, а размер уже нарисован
  выносными, размерной линией и стрелками, которые `dimensions_from_kernel`
  кладёт отдельными сущностями;
* подпись размера ставится ЦЕНТРОМ в точку над размерной линией, а
  `draw_text_entities` считает эту точку левым краем базовой линии — число
  уезжает вправо на половину своей ширины.

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

    for entity in ir.entities:
        if getattr(entity, "construction", False):
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
            r = float(entity.radius) * scale
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=stroke(entity))
        elif isinstance(entity, Arc):
            cx, cy = xy(entity.center)
            r = float(entity.radius) * scale
            # IR: градусы против часовой в пространстве изображения (y вниз);
            # PIL.arc идёт по часовой — меняем знак и порядок концов.
            draw.arc(
                [cx - r, cy - r, cx + r, cy + r],
                start=-float(entity.end_angle),
                end=-float(entity.start_angle),
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
            box = draw.textbbox((0, 0), entity.text, font=font, anchor="ms")
            text_width = box[2] - box[0]
            # Центр по горизонтали, базовая линия — в точке: так подпись стоит
            # над размерной линией, как задумано в `dimensions_from_kernel`.
            draw.text((cx, baseline), entity.text, fill=0, font=font, anchor="ms")
            drawn = draw.textbbox((cx, baseline), entity.text, font=font, anchor="ms")
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
