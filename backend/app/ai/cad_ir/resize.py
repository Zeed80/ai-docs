"""Pixel-space CadIR resizing without changing physical drawing dimensions."""

from __future__ import annotations

from app.ai.cad_ir.schema import CadIR, Point, SourceRegion


def _scale_point(point: Point, factor: float) -> None:
    point.x *= factor
    point.y *= factor


def _scale_region(region: SourceRegion | None, factor: float) -> None:
    if region is None:
        return
    region.x0 *= factor
    region.y0 *= factor
    region.x1 *= factor
    region.y1 *= factor


def resize_ir(ir: CadIR, width: int, height: int) -> CadIR:
    """Return a resized deep copy whose entities still describe the same sheet.

    Coordinates live in source pixels, while ``scale`` is millimetres per
    pixel. Resizing therefore changes every pixel-space coordinate and applies
    the inverse factor to the physical scale.
    """
    old_width = ir.source.image_width
    old_height = ir.source.image_height
    factor_x = width / old_width
    factor_y = height / old_height
    # Relative tolerance: an absolute one rejects legitimate large up/downscales
    # where integer target dimensions cannot hold the ratio exactly (e.g.
    # 112×100 → 1024×914 drifts 0.003 in absolute factor but < 0.05% in ratio).
    if abs(factor_x - factor_y) > 0.01 * max(factor_x, factor_y):
        raise ValueError("CadIR resize must preserve aspect ratio")
    factor = (factor_x + factor_y) / 2
    out = ir.model_copy(deep=True)
    out.source.image_width = width
    out.source.image_height = height
    if out.scale is not None:
        out.scale /= factor

    for entity in out.entities:
        for attr in ("p1", "p2", "center", "position", "leader"):
            point = getattr(entity, attr, None)
            if point is not None:
                _scale_point(point, factor)
        for attr in ("points", "boundary"):
            for point in getattr(entity, attr, []) or []:
                _scale_point(point, factor)
        for hole in getattr(entity, "holes", []) or []:
            for point in hole:
                _scale_point(point, factor)
        if hasattr(entity, "radius"):
            entity.radius *= factor
        if hasattr(entity, "height"):
            entity.height *= factor
        _scale_region(entity.source_region, factor)

    if out.sheet.frame_px:
        out.sheet.frame_px = [value * factor for value in out.sheet.frame_px]
    for unresolved in out.unresolved_regions:
        _scale_region(unresolved.region, factor)
    return out


def fit_ir_to_long_side(ir: CadIR, long_side: int) -> CadIR:
    """Resize ``ir`` only when its raster frame exceeds ``long_side``."""
    if long_side <= 0:
        raise ValueError("long_side must be positive")
    current = max(ir.source.image_width, ir.source.image_height)
    if current <= long_side:
        return ir.model_copy(deep=True)
    factor = long_side / current
    width = max(1, round(ir.source.image_width * factor))
    height = max(1, round(ir.source.image_height * factor))
    return resize_ir(ir, width, height)


def ensure_min_long_side(ir: CadIR, min_long_side: int) -> CadIR:
    """Upscale ``ir`` only when its raster frame is below ``min_long_side``.

    A physically tiny drawing (an M4 bolt is a few millimetres) whose native
    DXF extents map to a ~100 px frame is unrecoverable at that resolution — a
    hexagon side spans ~5 px, below what any recognizer can resolve. A real
    scan of the same sheet on A4 at 300 DPI is ~2500 px, so the tiny render is
    the unrealistic one. This floor renders such drawings at a resolution where
    their smallest primitives are actually resolvable, without ever shrinking a
    drawing that is already large enough.
    """
    if min_long_side <= 0:
        raise ValueError("min_long_side must be positive")
    current = max(ir.source.image_width, ir.source.image_height)
    if current >= min_long_side:
        return ir.model_copy(deep=True)
    factor = min_long_side / current
    width = max(1, round(ir.source.image_width * factor))
    height = max(1, round(ir.source.image_height * factor))
    return resize_ir(ir, width, height)
