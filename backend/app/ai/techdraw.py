"""Deterministic technical drawing generator (2D, ГОСТ/ЕСКД-style).

Why this exists: diffusion models (Qwen-Image etc.) cannot produce metrically
exact drawings — text comes out as gibberish and dimensions are not to scale.
A real technical drawing with exact dimensions, tolerances (квалитеты), surface
roughness (Ra) and a ГОСТ title block must be drawn by code from a structured
spec. The agent/LLM produces the spec (it's good at that); this module renders it
precisely to SVG (crisp, exact) → PNG, and to DXF (CAD-editable).

Supported part types (v1): ``shaft`` (stepped shaft / вал) and ``plate``
(rectangular or circular plate / flange with holes / фланец).
"""

from __future__ import annotations

import io
import math
from typing import Literal

import svgwrite
from pydantic import BaseModel, Field

# ── Spec models ──────────────────────────────────────────────────────────────


class TitleBlock(BaseModel):
    name: str = "Деталь"            # наименование
    designation: str = ""           # обозначение (децимальный номер)
    material: str = ""              # материал
    scale: str = ""                # масштаб (auto if empty)
    mass: str = ""                 # масса
    developer: str = ""            # разработал
    company: str = "AI-DOCS"


class ShaftSegment(BaseModel):
    diameter: float                 # Ø, мм
    length: float                   # длина ступени, мм
    tolerance: str = ""            # квалитет/посадка, напр. "h6", "k6", "H7"
    roughness: float | None = None  # Ra, мкм
    chamfer: float = 0.0           # фаска, мм (45°)
    thread: str = ""               # резьба, напр. "M20×1.5"


class ShaftSpec(BaseModel):
    type: Literal["shaft"] = "shaft"
    segments: list[ShaftSegment]
    title: TitleBlock = Field(default_factory=TitleBlock)


class Hole(BaseModel):
    x: float                        # позиция от центра, мм
    y: float
    diameter: float                 # Ø, мм
    tolerance: str = ""            # напр. "H7"
    counterbore: float = 0.0


class PlateSpec(BaseModel):
    type: Literal["plate"] = "plate"
    shape: Literal["rect", "circle"] = "rect"
    width: float = 100.0            # для rect
    height: float = 80.0           # для rect
    diameter: float = 100.0        # для circle
    thickness: float = 10.0
    thickness_tol: str = ""
    holes: list[Hole] = Field(default_factory=list)
    bolt_circle_d: float = 0.0     # делительная окружность, мм (0 = нет)
    bolt_circle_n: int = 0
    bolt_hole_d: float = 0.0
    bolt_hole_tol: str = ""
    roughness: float | None = None
    title: TitleBlock = Field(default_factory=TitleBlock)


# ── Render constants ─────────────────────────────────────────────────────────

PX = 3.78                            # px per mm at 96 dpi (1 mm ≈ 3.78 px)
SHEET_W, SHEET_H = 297, 210         # A4 landscape, mm
MARGIN = 12
TB_W, TB_H = 185, 55                # ГОСТ form 1 main inscription, mm
LINE = "#111"
THIN = 0.35
THICK = 0.7
_STD_SCALES = [(100, 1), (50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1),
               (1, 2), (1, 2.5), (1, 4), (1, 5), (1, 10), (1, 20), (1, 50)]


def _pick_scale(extent_mm: float, avail_mm: float) -> tuple[float, str]:
    """Return (scale_factor, label) so extent fits avail, from standard ratios."""
    for num, den in _STD_SCALES:
        f = num / den
        if extent_mm * f <= avail_mm:
            label = f"{int(num) if num==int(num) else num}:{int(den) if den==int(den) else den}"
            return f, label
    return 1 / 100, "1:100"


def _arrow(dwg, g, x, y, ang):
    a = 2.2
    for s in (0.4, -0.4):
        dx = a * math.cos(ang + s)
        dy = a * math.sin(ang + s)
        g.add(dwg.line((x, y), (x - dx, y - dy), stroke=LINE, stroke_width=THIN))


def _txt(dwg, g, x, y, s, size=3.0, anchor="middle", rot=0):
    t = dwg.text(s, insert=(x, y), font_size=f"{size}", font_family="Arial",
                 fill=LINE, text_anchor=anchor)
    if rot:
        t.rotate(rot, center=(x, y))
    g.add(t)


def _roughness_symbol(dwg, g, x, y, ra: float):
    """ГОСТ 2.309 roughness tick (√) with Ra value, apex at (x,y)."""
    g.add(dwg.line((x, y), (x - 2.5, y + 4.3), stroke=LINE, stroke_width=THIN))
    g.add(dwg.line((x, y), (x + 5.0, y - 4.3), stroke=LINE, stroke_width=THIN))
    g.add(dwg.line((x + 5.0, y - 4.3), (x + 11.0, y - 4.3), stroke=LINE, stroke_width=THIN))
    _txt(dwg, g, x + 8.0, y - 5.0, f"Ra {ra:g}", size=2.6)


DIA = "Ø"  # U+00D8 — widely-supported diameter sign (⌀ U+2300 lacks font coverage)


def _dim_label(nominal: float, tol: str, prefix: str = "") -> str:
    if prefix == "⌀":
        prefix = DIA
    return f"{prefix}{nominal:g}{tol}"


# ── Shaft ────────────────────────────────────────────────────────────────────


def _render_shaft(spec: ShaftSpec) -> str:
    total_len = sum(s.length for s in spec.segments)
    max_d = max(s.diameter for s in spec.segments)
    avail_w = SHEET_W - 2 * MARGIN - 30
    avail_h = SHEET_H - 2 * MARGIN - TB_H - 60
    sf, scale_label = _pick_scale(max(total_len, max_d), min(avail_w, avail_h * 2))

    dwg = svgwrite.Drawing(size=(f"{SHEET_W*PX}px", f"{SHEET_H*PX}px"),
                           viewBox=f"0 0 {SHEET_W} {SHEET_H}")
    dwg.add(dwg.rect((0, 0), (SHEET_W, SHEET_H), fill="white"))
    # sheet frame (ГОСТ 2.301): 20mm left, 5mm others
    dwg.add(dwg.rect((20, 5), (SHEET_W - 25, SHEET_H - 10), fill="none",
                     stroke=LINE, stroke_width=THICK))
    g = dwg.g()

    cx0 = 35
    cy = MARGIN + 25 + (max_d * sf) / 2
    x = cx0
    centerline_y = cy
    rough_above_y = cy - (max_d * sf) / 2 - 16

    # contour
    prev_h = None
    for seg in spec.segments:
        w = seg.length * sf
        h = seg.diameter * sf
        top = cy - h / 2
        bot = cy + h / 2
        # step vertical lines
        if prev_h is None or abs(prev_h - h) > 1e-6:
            yh = (prev_h or h) / 2
            g.add(dwg.line((x, cy - max(yh, h / 2)), (x, cy + max(yh, h / 2)),
                           stroke=LINE, stroke_width=THICK))
        # top/bottom contour
        ch = seg.chamfer * sf
        g.add(dwg.line((x + ch, top), (x + w - ch, top), stroke=LINE, stroke_width=THICK))
        g.add(dwg.line((x + ch, bot), (x + w - ch, bot), stroke=LINE, stroke_width=THICK))
        if ch > 0:  # chamfers
            g.add(dwg.line((x, top + ch), (x + ch, top), stroke=LINE, stroke_width=THICK))
            g.add(dwg.line((x, bot - ch), (x + ch, bot), stroke=LINE, stroke_width=THICK))
            g.add(dwg.line((x + w - ch, top), (x + w, top + ch), stroke=LINE, stroke_width=THICK))
            g.add(dwg.line((x + w - ch, bot), (x + w, bot - ch), stroke=LINE, stroke_width=THICK))
        prev_h = h
        x += w
    # right end cap
    g.add(dwg.line((x, cy - prev_h / 2), (x, cy + prev_h / 2), stroke=LINE, stroke_width=THICK))

    # centerline (dash-dot)
    g.add(dwg.line((cx0 - 6, centerline_y), (x + 6, centerline_y), stroke=LINE,
                   stroke_width=THIN, stroke_dasharray="8,2,1,2"))

    # diameter callouts (stacked above) + roughness
    x = cx0
    level = 0
    for seg in spec.segments:
        w = seg.length * sf
        mx = x + w / 2
        top = cy - (seg.diameter * sf) / 2
        label = _dim_label(seg.diameter, seg.tolerance, "⌀")
        if seg.thread:
            label = seg.thread
        ly = rough_above_y - (level % 2) * 7
        g.add(dwg.line((mx, top), (mx, ly + 2), stroke=LINE, stroke_width=THIN))
        _txt(dwg, g, mx, ly, label, size=3.2)
        if seg.roughness is not None:
            _roughness_symbol(dwg, g, mx + 6, top - 1, seg.roughness)
        level += 1
        x += w

    # length dimension chain (below)
    dim_y = cy + (max_d * sf) / 2 + 16
    x = cx0
    for seg in spec.segments:
        w = seg.length * sf
        g.add(dwg.line((x, dim_y - 3), (x, dim_y + 3), stroke=LINE, stroke_width=THIN))
        g.add(dwg.line((x, dim_y), (x + w, dim_y), stroke=LINE, stroke_width=THIN))
        _arrow(dwg, g, x, dim_y, 0)
        _arrow(dwg, g, x + w, dim_y, math.pi)
        _txt(dwg, g, x + w / 2, dim_y - 1.5, f"{seg.length:g}", size=3.0)
        x += w
    g.add(dwg.line((x, dim_y - 3), (x, dim_y + 3), stroke=LINE, stroke_width=THIN))
    # overall length
    oy = dim_y + 10
    g.add(dwg.line((cx0, oy), (x, oy), stroke=LINE, stroke_width=THIN))
    _arrow(dwg, g, cx0, oy, 0)
    _arrow(dwg, g, x, oy, math.pi)
    _txt(dwg, g, (cx0 + x) / 2, oy - 1.5, f"{total_len:g}", size=3.2)

    dwg.add(g)
    _title_block(dwg, spec.title, scale_label, "вал")
    return dwg.tostring()


# ── Plate / flange ───────────────────────────────────────────────────────────


def _render_plate(spec: PlateSpec) -> str:
    extent = spec.diameter if spec.shape == "circle" else max(spec.width, spec.height)
    avail = min(SHEET_W - 2 * MARGIN - 40, SHEET_H - 2 * MARGIN - TB_H - 40)
    sf, scale_label = _pick_scale(extent, avail)

    dwg = svgwrite.Drawing(size=(f"{SHEET_W*PX}px", f"{SHEET_H*PX}px"),
                           viewBox=f"0 0 {SHEET_W} {SHEET_H}")
    dwg.add(dwg.rect((0, 0), (SHEET_W, SHEET_H), fill="white"))
    dwg.add(dwg.rect((20, 5), (SHEET_W - 25, SHEET_H - 10), fill="none",
                     stroke=LINE, stroke_width=THICK))
    g = dwg.g()

    cx = 110
    cy = 95

    def cm(px, py):  # center marks (dash-dot cross)
        g.add(dwg.line((px - 5, py), (px + 5, py), stroke=LINE, stroke_width=THIN,
                       stroke_dasharray="4,1,1,1"))
        g.add(dwg.line((px, py - 5), (px, py + 5), stroke=LINE, stroke_width=THIN,
                       stroke_dasharray="4,1,1,1"))

    if spec.shape == "circle":
        r = spec.diameter * sf / 2
        g.add(dwg.circle((cx, cy), r, fill="none", stroke=LINE, stroke_width=THICK))
        cm(cx, cy)
        # ⌀ outer
        g.add(dwg.line((cx - r, cy + r + 10), (cx + r, cy + r + 10), stroke=LINE, stroke_width=THIN))
        _arrow(dwg, g, cx - r, cy + r + 10, 0)
        _arrow(dwg, g, cx + r, cy + r + 10, math.pi)
        _txt(dwg, g, cx, cy + r + 8.5, _dim_label(spec.diameter, "", "⌀"), size=3.2)
    else:
        w = spec.width * sf
        h = spec.height * sf
        g.add(dwg.rect((cx - w / 2, cy - h / 2), (w, h), fill="none",
                       stroke=LINE, stroke_width=THICK))
        cm(cx, cy)
        # width + height dims
        dy = cy + h / 2 + 12
        g.add(dwg.line((cx - w / 2, dy), (cx + w / 2, dy), stroke=LINE, stroke_width=THIN))
        _arrow(dwg, g, cx - w / 2, dy, 0); _arrow(dwg, g, cx + w / 2, dy, math.pi)
        _txt(dwg, g, cx, dy - 1.5, f"{spec.width:g}", size=3.2)
        dx = cx + w / 2 + 12
        g.add(dwg.line((dx, cy - h / 2), (dx, cy + h / 2), stroke=LINE, stroke_width=THIN))
        _arrow(dwg, g, dx, cy - h / 2, math.pi / 2); _arrow(dwg, g, dx, cy + h / 2, -math.pi / 2)
        _txt(dwg, g, dx + 4, cy, f"{spec.height:g}", size=3.2, rot=90)

    # explicit holes
    for hole in spec.holes:
        hx = cx + hole.x * sf
        hy = cy - hole.y * sf
        hr = hole.diameter * sf / 2
        g.add(dwg.circle((hx, hy), hr, fill="none", stroke=LINE, stroke_width=THICK))
        cm(hx, hy)
        g.add(dwg.line((hx + hr * 0.7, hy - hr * 0.7), (hx + hr + 8, hy - hr - 8),
                       stroke=LINE, stroke_width=THIN))
        _txt(dwg, g, hx + hr + 16, hy - hr - 9, _dim_label(hole.diameter, hole.tolerance, "⌀"),
             size=2.8, anchor="middle")

    # bolt circle
    if spec.bolt_circle_d > 0 and spec.bolt_circle_n > 0:
        bcr = spec.bolt_circle_d * sf / 2
        g.add(dwg.circle((cx, cy), bcr, fill="none", stroke=LINE, stroke_width=THIN,
                         stroke_dasharray="6,2,1,2"))
        for i in range(spec.bolt_circle_n):
            ang = 2 * math.pi * i / spec.bolt_circle_n - math.pi / 2
            hx = cx + bcr * math.cos(ang)
            hy = cy + bcr * math.sin(ang)
            hr = spec.bolt_hole_d * sf / 2
            g.add(dwg.circle((hx, hy), max(hr, 1.2), fill="none", stroke=LINE, stroke_width=THICK))
            cm(hx, hy)
        _txt(dwg, g, cx, cy - bcr - 4,
             f"{spec.bolt_circle_n}×{_dim_label(spec.bolt_hole_d, spec.bolt_hole_tol, '⌀')}",
             size=2.8)
        # bolt-circle diameter on a 45° leader to a clear spot below-left
        lx, ly = cx - bcr * 0.71, cy + bcr * 0.71
        _txt(dwg, g, lx - 10, ly + 6, _dim_label(spec.bolt_circle_d, "", "⌀"), size=2.8)

    # side view (thickness) to the right
    sv_x = cx + (extent * sf) / 2 + 38
    th = spec.thickness * sf
    sh = (spec.diameter if spec.shape == "circle" else spec.height) * sf
    g.add(dwg.rect((sv_x, cy - sh / 2), (th, sh), fill="none", stroke=LINE, stroke_width=THICK))
    g.add(dwg.line((sv_x, cy + sh / 2 + 8), (sv_x + th, cy + sh / 2 + 8), stroke=LINE, stroke_width=THIN))
    _txt(dwg, g, sv_x + th / 2, cy + sh / 2 + 6.5, _dim_label(spec.thickness, spec.thickness_tol), size=2.8)

    if spec.roughness is not None:
        _roughness_symbol(dwg, g, SHEET_W - 70, 16, spec.roughness)

    dwg.add(g)
    _title_block(dwg, spec.title, scale_label, "плита")
    return dwg.tostring()


# ── ГОСТ title block (form 1, simplified) ────────────────────────────────────


def _title_block(dwg, tb: TitleBlock, scale_label: str, default_kind: str):
    x0 = SHEET_W - 25 - TB_W
    y0 = SHEET_H - 10 - TB_H
    g = dwg.g()
    g.add(dwg.rect((x0, y0), (TB_W, TB_H), fill="none", stroke=LINE, stroke_width=THICK))
    # horizontal rules
    for yy in (8, 16, 24, 32, 40, 47):
        g.add(dwg.line((x0, y0 + yy), (x0 + TB_W, y0 + yy), stroke=LINE, stroke_width=THIN))
    # left label column / right name+designation block
    g.add(dwg.line((x0 + 65, y0), (x0 + 65, y0 + 47), stroke=LINE, stroke_width=THIN))
    g.add(dwg.line((x0 + 135, y0), (x0 + 135, y0 + TB_H), stroke=LINE, stroke_width=THIN))

    _txt(dwg, g, x0 + 100, y0 + 14, tb.name or default_kind, size=4.2)
    _txt(dwg, g, x0 + 100, y0 + 30, tb.designation or "—", size=4.0)
    # right column: material / scale / mass
    _txt(dwg, g, x0 + 160, y0 + 6, "Материал", size=2.4)
    _txt(dwg, g, x0 + 160, y0 + 12, (tb.material or "—")[:32], size=2.3)
    _txt(dwg, g, x0 + 145, y0 + 52, "Масштаб", size=2.4, anchor="start")
    _txt(dwg, g, x0 + 168, y0 + 52, tb.scale or scale_label, size=3.4)
    # bottom-left signatures area
    for lab, yy in (("Разраб.", 6), ("Пров.", 14), ("Н.контр.", 22)):
        _txt(dwg, g, x0 + 2, y0 + yy, lab, size=2.4, anchor="start")
    _txt(dwg, g, x0 + 30, y0 + 6, (tb.developer or "")[:14], size=2.6, anchor="start")
    _txt(dwg, g, x0 + 100, y0 + 53, tb.company, size=2.6)
    dwg.add(g)


# ── Public API ───────────────────────────────────────────────────────────────


def _render_shaft_iso(spec: ShaftSpec) -> str:
    """Pictorial 3D view of a shaft: cylinders with elliptical ends + step rings."""
    total_len = sum(s.length for s in spec.segments)
    max_d = max(s.diameter for s in spec.segments)
    avail_w = SHEET_W - 2 * MARGIN - 40
    avail_h = SHEET_H - 2 * MARGIN - TB_H - 30
    sf, scale_label = _pick_scale(max(total_len * 1.1, max_d), min(avail_w, avail_h * 2.2))

    dwg = svgwrite.Drawing(size=(f"{SHEET_W*PX}px", f"{SHEET_H*PX}px"),
                           viewBox=f"0 0 {SHEET_W} {SHEET_H}")
    dwg.add(dwg.rect((0, 0), (SHEET_W, SHEET_H), fill="white"))
    dwg.add(dwg.rect((20, 5), (SHEET_W - 25, SHEET_H - 10), fill="none",
                     stroke=LINE, stroke_width=THICK))
    g = dwg.g()

    cx0 = 45
    cy = MARGIN + 30 + (max_d * sf) / 2
    x = cx0
    ell_rx = lambda d: max(d * sf * 0.16, 2.0)  # noqa: E731

    segs = spec.segments
    for i, seg in enumerate(segs):
        w = seg.length * sf
        ry = seg.diameter * sf / 2
        rx = ell_rx(seg.diameter)
        top, bot = cy - ry, cy + ry
        # body
        g.add(dwg.line((x, top), (x + w, top), stroke=LINE, stroke_width=THICK))
        g.add(dwg.line((x, bot), (x + w, bot), stroke=LINE, stroke_width=THICK))
        # left ring (step face) — full ellipse if first or diameter grows
        prev_ry = (segs[i - 1].diameter * sf / 2) if i > 0 else 0
        if i == 0 or ry > prev_ry:
            g.add(dwg.ellipse((x, cy), (rx, ry), fill="white", stroke=LINE, stroke_width=THIN))
        else:
            # smaller step: draw visible front arc of this ellipse
            g.add(dwg.path(d=f"M {x} {top} A {rx} {ry} 0 0 0 {x} {bot}",
                           fill="none", stroke=LINE, stroke_width=THIN))
        # right end cap ellipse (front, solid)
        g.add(dwg.ellipse((x + w, cy), (rx, ry), fill="white", stroke=LINE, stroke_width=THICK))
        # subtle shading lines for 3D feel
        for fr in (0.55, 0.72):
            yy = cy - ry + 2 * ry * fr
            g.add(dwg.line((x + rx * 0.4, yy), (x + w, yy), stroke="#bbb", stroke_width=0.2))
        # callout
        label = seg.thread or _dim_label(seg.diameter, seg.tolerance, "⌀")
        _txt(dwg, g, x + w / 2, top - 3, label, size=3.0)
        x += w

    _txt(dwg, g, (cx0 + x) / 2, cy + max_d * sf / 2 + 12,
         f"Изометрия · L={total_len:g}", size=3.0)
    dwg.add(g)
    _title_block(dwg, spec.title, scale_label, "вал")
    return dwg.tostring()


def _render_plate_iso(spec: PlateSpec) -> str:
    """Pictorial 3D view of a plate/flange: extruded prism/cylinder with depth."""
    extent = spec.diameter if spec.shape == "circle" else max(spec.width, spec.height)
    sf, scale_label = _pick_scale(extent * 1.3, SHEET_W - 2 * MARGIN - 60)
    dwg = svgwrite.Drawing(size=(f"{SHEET_W*PX}px", f"{SHEET_H*PX}px"),
                           viewBox=f"0 0 {SHEET_W} {SHEET_H}")
    dwg.add(dwg.rect((0, 0), (SHEET_W, SHEET_H), fill="white"))
    dwg.add(dwg.rect((20, 5), (SHEET_W - 25, SHEET_H - 10), fill="none",
                     stroke=LINE, stroke_width=THICK))
    g = dwg.g()
    cx, cy = 110, 90
    depth = spec.thickness * sf
    dxv, dyv = depth * 0.7, -depth * 0.4  # iso offset

    if spec.shape == "circle":
        r = spec.diameter * sf / 2
        g.add(dwg.ellipse((cx, cy), (r, r * 0.55), fill="white", stroke=LINE, stroke_width=THICK))
        g.add(dwg.ellipse((cx + dxv, cy + dyv), (r, r * 0.55), fill="none",
                          stroke=LINE, stroke_width=THIN))
        g.add(dwg.line((cx - r, cy), (cx - r + dxv, cy + dyv), stroke=LINE, stroke_width=THICK))
        g.add(dwg.line((cx + r, cy), (cx + r + dxv, cy + dyv), stroke=LINE, stroke_width=THICK))
        if spec.holes:
            hr = spec.holes[0].diameter * sf / 2
            g.add(dwg.ellipse((cx, cy), (hr, hr * 0.55), fill="white", stroke=LINE, stroke_width=THIN))
        _txt(dwg, g, cx, cy + r * 0.55 + 10, _dim_label(spec.diameter, "", "⌀"), size=3.2)
    else:
        w, h = spec.width * sf, spec.height * sf * 0.6
        g.add(dwg.rect((cx - w / 2, cy - h / 2), (w, h), fill="white", stroke=LINE, stroke_width=THICK))
        g.add(dwg.polygon([(cx - w / 2, cy - h / 2), (cx - w / 2 + dxv, cy - h / 2 + dyv),
                           (cx + w / 2 + dxv, cy - h / 2 + dyv), (cx + w / 2, cy - h / 2)],
                          fill="#f4f4f4", stroke=LINE, stroke_width=THIN))
        g.add(dwg.polygon([(cx + w / 2, cy - h / 2), (cx + w / 2 + dxv, cy - h / 2 + dyv),
                           (cx + w / 2 + dxv, cy + h / 2 + dyv), (cx + w / 2, cy + h / 2)],
                          fill="#e9e9e9", stroke=LINE, stroke_width=THIN))
        _txt(dwg, g, cx, cy + h / 2 + 10, f"{spec.width:g}×{spec.height:g}×{spec.thickness:g}", size=3.0)

    _txt(dwg, g, cx, 24, "Изометрия", size=3.0)
    dwg.add(g)
    _title_block(dwg, spec.title, scale_label, "плита")
    return dwg.tostring()


SPEC_SYSTEM_PROMPT = (
    "Ты — инженер-конструктор. По описанию детали верни СТРОГО JSON-спецификацию "
    "для построения точного технического чертежа (ЕСКД). Без пояснений, только JSON.\n\n"
    "Тип 'shaft' (вал/ось — ступенчатое тело вращения):\n"
    '{"type":"shaft","segments":[{"diameter":50,"length":40,"tolerance":"h6",'
    '"roughness":0.8,"chamfer":1.5,"thread":""}],'
    '"title":{"name":"...","designation":"","material":"Сталь 45 ГОСТ 1050-2013","developer":""}}\n'
    "Тип 'plate' (плита/фланец/крышка — плоская деталь):\n"
    '{"type":"plate","shape":"circle|rect","diameter":120,"width":100,"height":80,'
    '"thickness":14,"thickness_tol":"h12","holes":[{"x":0,"y":0,"diameter":40,"tolerance":"H7"}],'
    '"bolt_circle_d":90,"bolt_circle_n":6,"bolt_hole_d":11,"bolt_hole_tol":"H12","roughness":3.2,'
    '"title":{"name":"...","material":"...","developer":""}}\n\n'
    "Правила: размеры в мм; tolerance — квалитет/посадка (h6,k6,H7,js6,...); roughness — Ra в мкм "
    "(0.8/1.6/3.2/6.3); thread — метрическая резьба ('M20×1.5') или '' если нет; "
    "для plate координаты отверстий x,y от центра. Подбирай реалистичные значения и материал по ГОСТ, "
    "если не заданы. Верни ОДИН JSON-объект."
)


def render_spec_to_svg(spec: dict, view: str = "front") -> str:
    kind = spec.get("type")
    iso = view in ("isometric", "iso", "3d")
    if kind == "shaft":
        return _render_shaft_iso(ShaftSpec(**spec)) if iso else _render_shaft(ShaftSpec(**spec))
    if kind == "plate":
        return _render_plate_iso(PlateSpec(**spec)) if iso else _render_plate(PlateSpec(**spec))
    raise ValueError(f"Неизвестный тип чертежа: {kind!r} (ожидается shaft|plate)")


def render_spec_to_png(spec: dict, scale: float = 2.0, view: str = "front") -> bytes:
    import cairosvg

    svg = render_spec_to_svg(spec, view=view)
    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), scale=scale)


def render_spec_to_dxf(spec: dict) -> bytes:
    """Export exact geometry to DXF (CAD-editable). Lines/circles + dimension text."""
    import ezdxf

    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    kind = spec.get("type")
    if kind == "shaft":
        s = ShaftSpec(**spec)
        x = 0.0
        cy = 0.0
        prev_h = None
        for seg in s.segments:
            w, h = seg.length, seg.diameter
            top, bot = cy + h / 2, cy - h / 2
            msp.add_line((x, top), (x + w, top))
            msp.add_line((x, bot), (x + w, bot))
            if prev_h is None or abs(prev_h - h) > 1e-9:
                yy = max((prev_h or h) / 2, h / 2)
                msp.add_line((x, cy - yy), (x, cy + yy))
            msp.add_text(_dim_label(seg.diameter, seg.tolerance, "⌀"),
                         height=h * 0.12 + 1).set_placement((x + w / 2, top + 3))
            prev_h = h
            x += w
        msp.add_line((x, cy - prev_h / 2), (x, cy + prev_h / 2))
    elif kind == "plate":
        s = PlateSpec(**spec)
        if s.shape == "circle":
            msp.add_circle((0, 0), s.diameter / 2)
        else:
            msp.add_lwpolyline([(-s.width / 2, -s.height / 2), (s.width / 2, -s.height / 2),
                                (s.width / 2, s.height / 2), (-s.width / 2, s.height / 2)],
                               close=True)
        for hole in s.holes:
            msp.add_circle((hole.x, hole.y), hole.diameter / 2)
        if s.bolt_circle_d > 0 and s.bolt_circle_n > 0:
            for i in range(s.bolt_circle_n):
                ang = 2 * math.pi * i / s.bolt_circle_n
                msp.add_circle((s.bolt_circle_d / 2 * math.cos(ang),
                                s.bolt_circle_d / 2 * math.sin(ang)), s.bolt_hole_d / 2)
    else:
        raise ValueError(f"Неизвестный тип чертежа: {kind!r}")
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")
