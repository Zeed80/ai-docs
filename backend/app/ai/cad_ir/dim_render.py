"""Shared geometry/label helpers for rendering ``DimensionEntity`` per
ГОСТ 2.307 conventions (filled arrowheads, Ø/R prefixes) — used by both
``svg_render`` (editor overlay) and ``dxf_render`` (CAD export).

Deliberately NOT used by ``png_render``: that rasterizer doubles as the
coverage-verification surface (``cad_recognize/verify.py`` scores recognized
geometry against it), so it stays geometry-only — adding filled arrowhead
triangles there would perturb recall/precision scoring for reasons unrelated
to this module.
"""

from __future__ import annotations

import math

from app.ai.cad_ir.schema import DimensionEntity

_ARROW_LEN_MM = 2.5
_ARROW_LEN_FALLBACK_PX = 8.0
_ARROW_WIDTH_RATIO = 0.28


def arrow_len_mm() -> float:
    """DXF is already in mm — arrow length is the ГОСТ constant directly,
    no scale conversion needed."""
    return _ARROW_LEN_MM


def arrow_len_px(scale: float | None) -> float:
    """``scale`` is mm-per-px (``CadIR.scale``); falls back to a fixed pixel
    length when the sheet has no known scale yet (``SCALE_UNKNOWN``)."""
    if scale:
        return _ARROW_LEN_MM / scale
    return _ARROW_LEN_FALLBACK_PX


def arrow_triangle(
    tip: tuple[float, float], direction: tuple[float, float], length: float
) -> list[tuple[float, float]]:
    """Filled arrowhead triangle: apex at ``tip``, pointing along the unit
    ``direction``, base ``length`` back along that direction."""
    dx, dy = direction
    norm = math.hypot(dx, dy) or 1.0
    dx, dy = dx / norm, dy / norm
    px, py = -dy, dx
    base_x = tip[0] - dx * length
    base_y = tip[1] - dy * length
    half_w = length * _ARROW_WIDTH_RATIO
    return [
        (tip[0], tip[1]),
        (base_x + px * half_w, base_y + py * half_w),
        (base_x - px * half_w, base_y - py * half_w),
    ]


def dimension_arrows_for_points(
    p1: tuple[float, float], p2: tuple[float, float], kind: str, length: float
) -> list[list[tuple[float, float]]]:
    """Arrowhead triangles for a dimension line from ``p1`` to ``p2``: two
    (both ends, pointing outward) for linear/diameter, one for radial
    (pointing at ``p2``, the point on the arc/circle). Coordinate-space
    agnostic — callers pass already-projected points (px for SVG, mm for
    DXF) and a matching ``length``."""
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    if kind == "radial":
        return [arrow_triangle(p2, (dx, dy), length)]
    return [
        arrow_triangle(p1, (-dx, -dy), length),
        arrow_triangle(p2, (dx, dy), length),
    ]


def dimension_arrows(entity: DimensionEntity, scale: float | None) -> list[list[tuple[float, float]]]:
    """SVG convenience wrapper: arrows in IR pixel space."""
    return dimension_arrows_for_points(
        (entity.p1.x, entity.p1.y), (entity.p2.x, entity.p2.y), entity.kind, arrow_len_px(scale)
    )


def dimension_label(entity: DimensionEntity) -> str:
    """Display text with the Ø/R kind prefix applied when the raw text
    doesn't already carry it (manual entry may type either the bare number
    or the full annotation)."""
    base = entity.text or ("" if entity.value_mm is None else _fmt(entity.value_mm))
    if not base:
        return ""
    upper = base.upper()
    if entity.kind == "diameter" and "⌀" not in base and "Ø" not in upper:
        return f"⌀{base}"
    if entity.kind == "radial" and not upper.lstrip().startswith("R"):
        return f"R{base}"
    return base


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")
