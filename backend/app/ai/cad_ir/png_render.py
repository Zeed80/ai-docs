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
                0, t, cv2.LINE_AA,
            )
        elif isinstance(entity, Circle):
            cv2.circle(
                canvas,
                (int(round(entity.center.x)), int(round(entity.center.y))),
                int(round(entity.radius)), 0, t, cv2.LINE_AA,
            )
        elif isinstance(entity, Arc):
            cv2.ellipse(
                canvas,
                (int(round(entity.center.x)), int(round(entity.center.y))),
                (int(round(entity.radius)), int(round(entity.radius))),
                0.0, entity.start_angle, entity.end_angle, 0, t, cv2.LINE_AA,
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
                harr = np.array(
                    [[int(round(p.x)), int(round(p.y))] for p in hole], dtype=np.int32
                )
                cv2.polylines(canvas, [harr], True, 0, t, cv2.LINE_AA)
        elif isinstance(entity, DimensionEntity):
            cv2.line(
                canvas,
                (int(round(entity.p1.x)), int(round(entity.p1.y))),
                (int(round(entity.p2.x)), int(round(entity.p2.y))),
                0, thin_px, cv2.LINE_AA,
            )
        elif isinstance(entity, TextEntity):
            continue
    return canvas


def render_ir_to_png(
    ir: CadIR,
    keep_raster: Any | None = None,
    thin_px: int = 1,
    thick_px: int = 2,
) -> bytes:
    """PNG preview: entities + passthrough raster regions."""
    import cv2
    import numpy as np

    canvas = rasterize_entities(
        ir.entities, ir.source.image_width, ir.source.image_height, thin_px, thick_px
    )
    if keep_raster is not None:
        canvas[np.asarray(keep_raster).astype(bool)] = 0
    ok, buf = cv2.imencode(".png", canvas)
    if not ok:
        raise RuntimeError("PNG encode failed")
    return buf.tobytes()
