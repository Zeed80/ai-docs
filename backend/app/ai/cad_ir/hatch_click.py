"""Click-to-hatch (Ф5.8): find the enclosed region under a click point and
turn it into a real HatchRegion entity.

Reuses the same rasterizer the coverage verifier and PNG preview already use
(``png_render.rasterize_entities``) so "enclosed" means exactly what it
looks like on screen — flood-fill from the click, bounded by whatever ink
the current entities draw, contour-traced back into a boundary polygon.
"""

from __future__ import annotations

from app.ai.cad_ir.schema import CadIR, HatchRegion, Point

_MIN_HATCH_CLICK_AREA_PX = 50.0
_SIMPLIFY_EPS = 2.0


def hatch_region_at_point(ir: CadIR, x: float, y: float) -> HatchRegion | None:
    """None when the click isn't inside a genuinely enclosed area: landed
    directly on ink, the flood spilled out to the sheet border (nothing
    closes the loop), or the enclosed area is too small to be a real hatch
    target (noise, not a region)."""
    import cv2
    import numpy as np

    from app.ai.cad_ir.png_render import rasterize_entities

    w, h = ir.source.image_width, ir.source.image_height
    xi, yi = int(round(x)), int(round(y))
    if not (0 <= xi < w and 0 <= yi < h):
        return None

    canvas = rasterize_entities(ir.entities, w, h, thin_px=1, thick_px=2)
    if canvas[yi, xi] == 0:
        return None  # clicked on a line, not inside a region

    mask = np.zeros((h + 2, w + 2), dtype=np.uint8)
    fill_value = 100
    flood = canvas.copy()
    cv2.floodFill(flood, mask, (xi, yi), fill_value)
    region = (flood == fill_value).astype(np.uint8)

    if region[0, :].any() or region[-1, :].any() or region[:, 0].any() or region[:, -1].any():
        return None  # flood reached the sheet edge — not actually enclosed

    contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < _MIN_HATCH_CLICK_AREA_PX:
        return None
    approx = cv2.approxPolyDP(largest, _SIMPLIFY_EPS, True)
    points = [Point(x=float(p[0][0]), y=float(p[0][1])) for p in approx]
    if len(points) < 3:
        return None
    return HatchRegion(
        boundary=points, pattern="ansi31", origin="human", assurance="human_approved"
    )
