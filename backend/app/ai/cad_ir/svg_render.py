"""CAD IR → SVG overlay for the review/editor UI.

Renders in IR pixel space (same as the source scan, y-down — no flipping),
so the frontend can stack it 1:1 over the source image. Every element
carries ``data-entity-id`` (click/hover targeting), ``data-line-class`` and
``data-confidence`` so the UI can highlight uncertain entities without
re-parsing geometry.
"""

from __future__ import annotations

import html
import math

from app.ai.cad_ir.annotations import annotation_text
from app.ai.cad_ir.dim_render import dimension_arrows, dimension_label
from app.ai.cad_ir.schema import (
    AnnotationEntity,
    Arc,
    CadIR,
    Circle,
    DimensionEntity,
    HatchRegion,
    Polyline,
    Segment,
    TextEntity,
)

_STROKE = {"main": 2.0, "thin": 1.0}
_DASH = {"axis": "12 3 3 3", "hidden": "8 4"}


def _attrs(entity, extra: str = "") -> str:
    dash = _DASH.get(entity.line_class)
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'data-entity-id="{entity.id}" data-line-class="{entity.line_class}" '
        f'data-confidence="{entity.confidence:.2f}" '
        f'stroke-width="{_STROKE[entity.width_class]}"{dash_attr}{extra}'
    )


def _fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def render_ir_to_svg(ir: CadIR) -> bytes:
    """Hand-built SVG (svgwrite rejects data-* attributes in strict mode and
    we control every byte anyway)."""
    w = ir.source.image_width
    h = ir.source.image_height
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" fill="none" stroke="currentColor" '
        f'stroke-linecap="round" stroke-linejoin="round">'
    ]
    for e in ir.entities:
        if getattr(e, "construction", False):
            continue  # A2: auxiliary geometry is canvas-only, not in the export
        if isinstance(e, Segment):
            parts.append(
                f'<line x1="{_fmt(e.p1.x)}" y1="{_fmt(e.p1.y)}" '
                f'x2="{_fmt(e.p2.x)}" y2="{_fmt(e.p2.y)}" {_attrs(e)}/>'
            )
        elif isinstance(e, Circle):
            parts.append(
                f'<circle cx="{_fmt(e.center.x)}" cy="{_fmt(e.center.y)}" '
                f'r="{_fmt(e.radius)}" {_attrs(e)}/>'
            )
        elif isinstance(e, Arc):
            parts.append(f'<path d="{_arc_path(e)}" {_attrs(e)}/>')
        elif isinstance(e, Polyline):
            pts = " ".join(f"{_fmt(p.x)},{_fmt(p.y)}" for p in e.points)
            tag = "polygon" if e.closed else "polyline"
            parts.append(f'<{tag} points="{pts}" {_attrs(e)}/>')
        elif isinstance(e, HatchRegion):
            # <polygon> can't represent holes; a <path> with fill-rule
            #="evenodd" and one "M...Z" subpath per loop (outer + holes)
            # renders holes as actual gaps, not painted-over.
            fill = ' fill-opacity="0.15" fill="currentColor" fill-rule="evenodd"'
            loops = [e.boundary, *e.holes]
            d = " ".join(
                "M " + " L ".join(f"{_fmt(p.x)} {_fmt(p.y)}" for p in loop) + " Z"
                for loop in loops
            )
            parts.append(f'<path d="{d}" {_attrs(e, fill)}/>')
        elif isinstance(e, TextEntity):
            transform = (
                f' transform="rotate({_fmt(e.rotation)} {_fmt(e.position.x)} {_fmt(e.position.y)})"'
                if e.rotation else ""
            )
            parts.append(
                f'<text x="{_fmt(e.position.x)}" y="{_fmt(e.position.y)}" '
                f'font-size="{_fmt(e.height)}" fill="currentColor" stroke="none" '
                f'{_attrs(e)}{transform}>{html.escape(e.text)}</text>'
            )
        elif isinstance(e, DimensionEntity):
            mx, my = (e.p1.x + e.p2.x) / 2, (e.p1.y + e.p2.y) / 2
            label_text = dimension_label(e)
            label = (
                f'<text x="{_fmt(mx)}" y="{_fmt(my - 4)}" font-size="12" '
                f'fill="currentColor" stroke="none" text-anchor="middle">{html.escape(label_text)}</text>'
                if label_text else ""
            )
            arrows = "".join(
                '<polygon points="'
                + " ".join(f"{_fmt(x)},{_fmt(y)}" for x, y in tri)
                + '" fill="currentColor" stroke="none"/>'
                for tri in dimension_arrows(e, ir.scale)
            )
            parts.append(
                f'<g {_attrs(e)}><line x1="{_fmt(e.p1.x)}" y1="{_fmt(e.p1.y)}" '
                f'x2="{_fmt(e.p2.x)}" y2="{_fmt(e.p2.y)}"/>{arrows}{label}</g>'
            )
        elif isinstance(e, AnnotationEntity):
            text = annotation_text(e.kind, e.value, e.symbol, e.datum_refs)
            x, y, h = e.position.x, e.position.y, e.height
            leader = (
                f'<line x1="{_fmt(x)}" y1="{_fmt(y)}" '
                f'x2="{_fmt(e.leader.x)}" y2="{_fmt(e.leader.y)}"/>'
                if e.leader else ""
            )
            # ГОСТ boxes the geometric-tolerance frame and the datum letter.
            box = ""
            if e.kind in ("tolerance", "datum"):
                w = max(h * len(text) * 0.62, h * 1.6)
                box = (
                    f'<rect x="{_fmt(x - h * 0.3)}" y="{_fmt(y - h)}" '
                    f'width="{_fmt(w)}" height="{_fmt(h * 1.4)}" fill="none"/>'
                )
            parts.append(
                f'<g {_attrs(e)}>{leader}{box}'
                f'<text x="{_fmt(x)}" y="{_fmt(y)}" font-size="{_fmt(h)}" '
                f'fill="currentColor" stroke="none">{html.escape(text)}</text></g>'
            )
    parts.append("</svg>")
    return "".join(parts).encode("utf-8")


def _arc_path(e: Arc) -> str:
    """SVG path for an image-space arc (y-down, angles as cv2 draws them)."""
    a0 = math.radians(e.start_angle)
    a1 = math.radians(e.end_angle)
    x0 = e.center.x + e.radius * math.cos(a0)
    y0 = e.center.y + e.radius * math.sin(a0)
    x1 = e.center.x + e.radius * math.cos(a1)
    y1 = e.center.y + e.radius * math.sin(a1)
    span = abs(e.end_angle - e.start_angle)
    large = 1 if span % 360 > 180 else 0
    sweep = 1 if e.end_angle > e.start_angle else 0
    return (
        f"M {_fmt(x0)} {_fmt(y0)} "
        f"A {_fmt(e.radius)} {_fmt(e.radius)} 0 {large} {sweep} {_fmt(x1)} {_fmt(y1)}"
    )
