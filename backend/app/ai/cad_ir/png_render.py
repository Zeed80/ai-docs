"""Deterministic raster render of CAD IR geometry (preview + verification).

The same rasterizer serves the pipeline's PNG preview and the coverage
verifier — determinism is the point: what the user previews is exactly what
was scored. Text/dimension labels are drawn by the SVG/DXF renders; in the
raster they normally arrive through ``keep_raster`` (original text pixels are
excluded from re-stroking and copied through).
"""

from __future__ import annotations

from typing import Any

from app.ai.cad_ir.schema import (
    AnnotationEntity,
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    Entity,
    HatchRegion,
    Polyline,
    Segment,
    TextEntity,
)


def _draw_hatch_lines(canvas, boundary, holes, thin_px: int, spacing_px: int = 12) -> None:
    """ГОСТ 2.306 hatching: 45-degree lines clipped to the cut material.

    The preview drew only the region's OUTLINE, so a sectioned part looked
    exactly like an unsectioned one — the single most recognizable feature of
    a machine drawing was missing from the picture the user is shown.
    """
    import cv2
    import numpy as np

    mask = np.zeros(canvas.shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [boundary], 255)
    for hole in holes:
        cv2.fillPoly(mask, [hole], 0)
    if not mask.any():
        return
    lines = np.zeros_like(mask)
    x0, y0, width, height = cv2.boundingRect(boundary)
    # y = x + c across the region: c runs from -(x0+width) to (y0+height).
    start = -(x0 + width)
    stop = y0 + height
    for offset in range(start, stop + spacing_px, spacing_px):
        cv2.line(lines, (x0, x0 + offset), (x0 + width, x0 + width + offset), 255, thin_px)
    canvas[(lines > 0) & (mask > 0)] = 0


def rasterize_entities(
    entities: list[Entity],
    width: int,
    height: int,
    thin_px: int = 1,
    thick_px: int = 2,
):
    """Draw IR geometry onto a white uint8 canvas (0 = ink). Text and
    dimension labels are skipped — they are OCR/VLM artifacts, not stroke
    geometry (dimension leader lines ARE drawn)."""
    import cv2
    import numpy as np

    canvas = np.full((height, width), 255, dtype=np.uint8)
    for entity in entities:
        t = thick_px if entity.width_class == "main" else thin_px
        if isinstance(entity, Segment):
            cv2.line(
                canvas,
                (int(round(entity.p1.x)), int(round(entity.p1.y))),
                (int(round(entity.p2.x)), int(round(entity.p2.y))),
                0,
                t,
                cv2.LINE_AA,
            )
        elif isinstance(entity, Circle):
            cv2.circle(
                canvas,
                (int(round(entity.center.x)), int(round(entity.center.y))),
                int(round(entity.radius)),
                0,
                t,
                cv2.LINE_AA,
            )
        elif isinstance(entity, Arc):
            cv2.ellipse(
                canvas,
                (int(round(entity.center.x)), int(round(entity.center.y))),
                (int(round(entity.radius)), int(round(entity.radius))),
                0.0,
                entity.start_angle,
                entity.end_angle,
                0,
                t,
                cv2.LINE_AA,
            )
        elif isinstance(entity, Polyline):
            arr = np.array(
                [[int(round(p.x)), int(round(p.y))] for p in entity.points], dtype=np.int32
            )
            cv2.polylines(canvas, [arr], entity.closed, 0, t, cv2.LINE_AA)
        elif isinstance(entity, HatchRegion):
            arr = np.array(
                [[int(round(p.x)), int(round(p.y))] for p in entity.boundary], dtype=np.int32
            )
            cv2.polylines(canvas, [arr], True, 0, t, cv2.LINE_AA)
            for hole in entity.holes:
                harr = np.array([[int(round(p.x)), int(round(p.y))] for p in hole], dtype=np.int32)
                cv2.polylines(canvas, [harr], True, 0, t, cv2.LINE_AA)
            _draw_hatch_lines(
                canvas,
                arr,
                [
                    np.array([[int(round(p.x)), int(round(p.y))] for p in hole], dtype=np.int32)
                    for hole in entity.holes
                ],
                thin_px,
            )
        elif isinstance(entity, DimensionEntity):
            cv2.line(
                canvas,
                (int(round(entity.p1.x)), int(round(entity.p1.y))),
                (int(round(entity.p2.x)), int(round(entity.p2.y))),
                0,
                thin_px,
                cv2.LINE_AA,
            )
        elif isinstance(entity, (TextEntity, AnnotationEntity)):
            continue  # annotations/text are not stroke geometry
    return canvas


_TEXT_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def draw_text_entities(canvas, entities: list[Entity]) -> int:
    """Stamp TextEntity labels onto a rasterized canvas. Returns how many
    were drawn.

    Deliberately absent from ``rasterize_entities``: that canvas is what the
    coverage verifier scores, and text there arrives as original pixels
    through ``keep_raster``, so drawing glyphs would double-count them. This
    is for the opposite direction — turning an IR back into a picture of a
    drawing, where a sheet with no lettering is not a drawing at all.

    OpenCV's Hershey fonts have no Cyrillic, so this goes through PIL with a
    real TrueType face; without one it draws nothing rather than mojibake.
    """
    import numpy as np

    texts = [e for e in entities if isinstance(e, TextEntity) and (e.text or "").strip()]
    if not texts:
        return 0
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return 0

    path = next((p for p in _TEXT_FONTS if __import__("os").path.exists(p)), None)
    if path is None:
        return 0

    image = Image.fromarray(canvas)
    draw = ImageDraw.Draw(image)
    drawn = 0
    for entity in texts:
        size = max(6, int(round(entity.height)))
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            return drawn
        # TextEntity.position is the baseline-left corner (that is where the
        # OCR path puts it: box left, box bottom); PIL anchors at the top.
        x, y = float(entity.position.x), float(entity.position.y) - size
        if abs(entity.rotation) < 1.0:
            draw.text((x, y), entity.text, fill=0, font=font)
        else:
            # Rotated labels (vertical dimension text) are drawn on their own
            # tile and pasted, since PIL cannot rotate text in place.
            box = draw.textbbox((0, 0), entity.text, font=font)
            tile = Image.new("L", (box[2] - box[0] + 4, box[3] - box[1] + 4), 255)
            ImageDraw.Draw(tile).text((2, 2), entity.text, fill=0, font=font)
            tile = tile.rotate(entity.rotation, expand=True, fillcolor=255)
            image.paste(
                Image.eval(tile, lambda v: v),
                (int(x), int(y)),
                Image.eval(tile, lambda v: 255 - v),
            )
        drawn += 1
    canvas[:, :] = np.asarray(image)
    return drawn


def render_ir_to_png(
    ir: CadIR,
    keep_raster: Any | None = None,
    thin_px: int = 1,
    thick_px: int = 2,
    draw_text: bool = False,
) -> bytes:
    """PNG preview: entities + passthrough raster regions.

    ``draw_text`` additionally stamps TextEntity labels — needed when the
    render has to stand in for a scanned sheet (benchmark ground truth),
    not when it is a preview of traced geometry.
    """
    import cv2
    import numpy as np

    canvas = rasterize_entities(
        ir.entities, ir.source.image_width, ir.source.image_height, thin_px, thick_px
    )
    if draw_text:
        draw_text_entities(canvas, ir.entities)
    if keep_raster is not None:
        canvas[np.asarray(keep_raster).astype(bool)] = 0
    ok, buf = cv2.imencode(".png", canvas)
    if not ok:
        raise RuntimeError("PNG encode failed")
    return buf.tobytes()
