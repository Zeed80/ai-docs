"""Deterministic technical drawing generator (2D, ГОСТ/ЕСКД-style).

Why this exists: diffusion models (Qwen-Image etc.) cannot produce metrically
exact drawings — text comes out as gibberish and dimensions are not to scale.
A real technical drawing with exact dimensions, tolerances (квалитеты), surface
roughness (Ra) and a ГОСТ title block must be drawn by code from a structured
spec. The agent/LLM produces the spec (it's good at that); this module renders it
precisely to SVG (crisp, exact) → PNG, and to DXF (CAD-editable).

Supported part types: ``shaft`` (stepped shaft / вал), ``plate`` (rectangular
or circular plate / flange with holes / фланец), and ``assembly`` (a small
sborka of shaft/plate components placed on one sheet, optionally with a BOM).
Views: ``front`` (2D orthographic), ``isometric`` (3D pictorial, shaft/plate
only), ``section``/``half_section`` (cut view with ГОСТ 2.306 hatching).

Engineering reference data (tolerances, Ra series, materials, metric threads,
sheet formats) lives in ``techdraw_reference.py`` — this module renders; it
does not invent engineering values.
"""

from __future__ import annotations

import io
import math
from typing import Literal

import svgwrite
import structlog
from pydantic import BaseModel, Field

from app.ai import techdraw_reference as tdref

logger = structlog.get_logger()

# ── Spec models ──────────────────────────────────────────────────────────────


class TitleBlock(BaseModel):
    name: str = "Деталь"            # наименование
    designation: str = ""           # обозначение (децимальный номер)
    material: str = ""              # материал
    scale: str = ""                # масштаб (auto if empty)
    mass_kg: float | None = None    # масса, кг
    developer: str = ""            # разработал
    checked_by: str = ""           # проверил
    norm_checked_by: str = ""      # нормоконтроль
    approved_by: str = ""          # утвердил
    date: str = ""                 # дата
    litera: str = ""               # литера (У, О1, ...)
    sheet_no: int = 1
    sheet_count: int = 1
    sheet_format: Literal["A4", "A3", "A2", "auto"] = "auto"
    company: str = "AI-DOCS"
    show_frame: bool = False       # ГОСТ 2.301 рамка листа + штамп формы 1 (2.104).
                                    # По умолчанию выключено: студия/агент отдают
                                    # только сам чертёж (вид+размеры+допуски+Ra —
                                    # то, что реально определяет ЕСКД-соответствие
                                    # содержания), без административного оформления
                                    # листа. Явно включить (show_frame=True) — для
                                    # печати на утверждённом бланке предприятия.


class ShaftSegment(BaseModel):
    diameter: float                 # Ø, мм
    length: float                   # длина ступени, мм
    tolerance: str = ""            # квалитет/посадка, напр. "h6", "k6", "H7"
    roughness: float | None = None  # Ra, мкм
    chamfer: float = 0.0           # фаска, мм (45°)
    thread: str = ""               # резьба, напр. "M20×1.5"
    bore_diameter: float = 0.0     # внутренняя расточка, мм (0 = сплошной)
    section_hatch: bool = False    # показать этот сегмент в разрезе
    thread_end_view: bool = False  # добавить торцевой вид резьбы (¾ окружности)


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


class ComponentPlacement(BaseModel):
    ref: str = "1"                  # позиция в спецификации ("поз.1")
    spec: dict                       # вложенный ShaftSpec|PlateSpec как dict
    x: float = 0.0                   # мм, центр компонента на листе сборки
    y: float = 0.0
    qty: int = 1


class BomRow(BaseModel):
    pos: int
    designation: str = ""
    name: str
    qty: int = 1
    material: str = ""
    note: str = ""


class AssemblySpec(BaseModel):
    type: Literal["assembly"] = "assembly"
    components: list[ComponentPlacement]
    bom: list[BomRow] = Field(default_factory=list)
    title: TitleBlock = Field(default_factory=TitleBlock)


# ── Render constants ─────────────────────────────────────────────────────────

PX = 3.78                            # px per mm at 96 dpi (1 mm ≈ 3.78 px)
MARGIN = 12
TB_W, TB_H = tdref.TITLE_BLOCK_W_MM, tdref.TITLE_BLOCK_H_MM  # ГОСТ 2.104 form 1
LINE = "#111"
THIN = 0.35
THICK = 0.7
_STD_SCALES = [(100, 1), (50, 1), (20, 1), (10, 1), (5, 1), (2, 1), (1, 1),
               (1, 2), (1, 2.5), (1, 4), (1, 5), (1, 10), (1, 20), (1, 50)]


def _sheet_for(title: TitleBlock, extent_mm: float) -> tdref.SheetFormat:
    prefer = None if title.sheet_format == "auto" else title.sheet_format
    return tdref.choose_sheet_format(extent_mm, prefer=prefer)


def _new_sheet(fmt: tdref.SheetFormat, frame: bool = False):
    """Blank sheet, optionally with the ГОСТ 2.301 frame (20mm left margin, 5mm
    others). ``frame=False`` (the default throughout this module) omits the
    border — callers doing so must also skip ``_title_block`` and should call
    ``_autocrop_png`` on the rendered PNG so the result isn't a mostly-blank
    sheet-sized canvas."""
    dwg = svgwrite.Drawing(size=(f"{fmt.width_mm*PX}px", f"{fmt.height_mm*PX}px"),
                           viewBox=f"0 0 {fmt.width_mm} {fmt.height_mm}")
    dwg.add(dwg.rect((0, 0), (fmt.width_mm, fmt.height_mm), fill="white"))
    if frame:
        dwg.add(dwg.rect((20, 5), (fmt.width_mm - 25, fmt.height_mm - 10), fill="none",
                         stroke=LINE, stroke_width=THICK))
    g = dwg.g()
    return dwg, g


def _avail_h(sheet: tdref.SheetFormat, show_frame: bool, extra: float) -> float:
    """Vertical space for the drawing itself: with the frame off there's no
    stamp to reserve room for, so the content can use (almost) the full sheet."""
    reserve = TB_H if show_frame else 0.0
    return sheet.height_mm - 2 * MARGIN - reserve - extra


def _scale_note(dwg, g, x: float, y: float, scale_label: str) -> None:
    """Scale is real ЕСКД content (ГОСТ 2.109 requires a stated scale) — when
    the stamp is off (where it normally lives), still print it next to the
    drawing so this information isn't silently lost."""
    _txt(dwg, g, x, y, f"М {scale_label}", size=3.0, anchor="start")


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


def _hatch_rect(dwg, g, x0: float, y0: float, x1: float, y1: float,
                 pitch: float = 2.5, angle_deg: float = 45.0):
    """Fill an axis-aligned rect with parallel 45°/-45° lines (ГОСТ 2.306)."""
    if x1 <= x0 or y1 <= y0 or pitch <= 0:
        return
    w, h = x1 - x0, y1 - y0
    sign = 1.0 if angle_deg > 0 else -1.0
    diag = w + h
    c = -diag
    while c <= diag:
        if sign > 0:
            xs, xe = max(0.0, -c), min(w, h - c)
        else:
            xs, xe = max(0.0, c - h), min(w, c)
        if xe > xs:
            if sign > 0:
                p1, p2 = (x0 + xs, y0 + xs + c), (x0 + xe, y0 + xe + c)
            else:
                p1, p2 = (x0 + xs, y0 + c - xs), (x0 + xe, y0 + c - xe)
            g.add(dwg.line(p1, p2, stroke=LINE, stroke_width=THIN))
        c += pitch


def _thread_end_view(dwg, g, cx: float, cy: float, major_r: float, minor_r: float):
    """¾-circle torцевой вид резьбы (ГОСТ 2.311): full major circle, thin 270° minor arc."""
    g.add(dwg.circle((cx, cy), major_r, fill="none", stroke=LINE, stroke_width=THICK))
    a0, a1 = math.radians(-75), math.radians(195)
    x0p, y0p = cx + minor_r * math.cos(a0), cy + minor_r * math.sin(a0)
    x1p, y1p = cx + minor_r * math.cos(a1), cy + minor_r * math.sin(a1)
    g.add(dwg.path(d=f"M {x0p} {y0p} A {minor_r} {minor_r} 0 1 1 {x1p} {y1p}",
                   fill="none", stroke=LINE, stroke_width=THIN))


DIA = "Ø"  # U+00D8 — widely-supported diameter sign (⌀ U+2300 lacks font coverage)


def _dim_label(nominal: float, tol: str, prefix: str = "") -> str:
    if prefix == "⌀":
        prefix = DIA
    return f"{prefix}{nominal:g}{tol}"


# ── Shaft ────────────────────────────────────────────────────────────────────


def _draw_shaft(dwg, g, spec: ShaftSpec, cx0: float, cy: float, sf: float,
                 mode: str = "front") -> float:
    """Draw shaft geometry at a given origin/scale. Returns the right-edge x."""
    max_d = max(s.diameter for s in spec.segments)
    rough_above_y = cy - (max_d * sf) / 2 - 16
    hatch = mode in ("section", "half_section")

    # contour
    x = cx0
    prev_h = None
    for seg in spec.segments:
        w = seg.length * sf
        h = seg.diameter * sf
        top = cy - h / 2
        bot = cy + h / 2
        if prev_h is None or abs(prev_h - h) > 1e-6:
            yh = (prev_h or h) / 2
            g.add(dwg.line((x, cy - max(yh, h / 2)), (x, cy + max(yh, h / 2)),
                           stroke=LINE, stroke_width=THICK))
        ch = seg.chamfer * sf
        g.add(dwg.line((x + ch, top), (x + w - ch, top), stroke=LINE, stroke_width=THICK))
        g.add(dwg.line((x + ch, bot), (x + w - ch, bot), stroke=LINE, stroke_width=THICK))
        if ch > 0:
            g.add(dwg.line((x, top + ch), (x + ch, top), stroke=LINE, stroke_width=THICK))
            g.add(dwg.line((x, bot - ch), (x + ch, bot), stroke=LINE, stroke_width=THICK))
            g.add(dwg.line((x + w - ch, top), (x + w, top + ch), stroke=LINE, stroke_width=THICK))
            g.add(dwg.line((x + w - ch, bot), (x + w, bot - ch), stroke=LINE, stroke_width=THICK))

        # Real thread geometry (ГОСТ 2.311): minor-diameter lines, not just a label.
        thread_spec = tdref.parse_thread(seg.thread) if seg.thread else None
        if thread_spec:
            minor_h = tdref.minor_diameter_mm(thread_spec) * sf
            mtop, mbot = cy - minor_h / 2, cy + minor_h / 2
            g.add(dwg.line((x, mtop), (x + w, mtop), stroke=LINE, stroke_width=THIN))
            g.add(dwg.line((x, mbot), (x + w, mbot), stroke=LINE, stroke_width=THIN))

        # Section hatching: whole segment if full "section"; only flagged
        # segments for "half_section" (bottom half only, split at centerline).
        if hatch and (mode == "section" or seg.section_hatch):
            bore_h = seg.bore_diameter * sf if seg.bore_diameter else 0.0
            material = tdref.classify_material(spec.title.material)
            pitch = tdref.hatch_pitch_mm(material, seg.diameter)
            if mode == "section":
                if bore_h > 0:
                    _hatch_rect(dwg, g, x + ch, top, x + w - ch, cy - bore_h / 2, pitch)
                    _hatch_rect(dwg, g, x + ch, cy + bore_h / 2, x + w - ch, bot, pitch)
                else:
                    _hatch_rect(dwg, g, x + ch, top, x + w - ch, bot, pitch)
            else:  # half_section: hatch bottom half only (top stays a plain view)
                lo = cy + bore_h / 2 if bore_h > 0 else cy
                _hatch_rect(dwg, g, x + ch, lo, x + w - ch, bot, pitch)
            if bore_h > 0:
                g.add(dwg.line((x, cy - bore_h / 2), (x + w, cy - bore_h / 2),
                               stroke=LINE, stroke_width=THIN))
                g.add(dwg.line((x, cy + bore_h / 2), (x + w, cy + bore_h / 2),
                               stroke=LINE, stroke_width=THIN))
        prev_h = h
        x += w
    g.add(dwg.line((x, cy - prev_h / 2), (x, cy + prev_h / 2), stroke=LINE, stroke_width=THICK))

    # centerline (dash-dot)
    g.add(dwg.line((cx0 - 6, cy), (x + 6, cy), stroke=LINE,
                   stroke_width=THIN, stroke_dasharray="8,2,1,2"))

    # diameter callouts (stacked above) + roughness
    xi = cx0
    level = 0
    end_view_drawn = False
    for seg in spec.segments:
        w = seg.length * sf
        mx = xi + w / 2
        top = cy - (seg.diameter * sf) / 2
        label = _dim_label(seg.diameter, seg.tolerance, "⌀")
        if seg.thread:
            label = seg.thread
        ly = rough_above_y - (level % 2) * 7
        g.add(dwg.line((mx, top), (mx, ly + 2), stroke=LINE, stroke_width=THIN))
        _txt(dwg, g, mx, ly, label, size=3.2)
        if seg.roughness is not None:
            _roughness_symbol(dwg, g, mx + 6, top - 1, seg.roughness)
        if seg.thread_end_view and not end_view_drawn:
            t = tdref.parse_thread(seg.thread)
            if t:
                evx, evy = x + 24, cy
                _thread_end_view(dwg, g, evx, evy, (seg.diameter * sf) / 2,
                                  tdref.minor_diameter_mm(t) * sf / 2)
                end_view_drawn = True
        level += 1
        xi += w

    # length dimension chain (below)
    dim_y = cy + (max_d * sf) / 2 + 16
    xi = cx0
    for seg in spec.segments:
        w = seg.length * sf
        g.add(dwg.line((xi, dim_y - 3), (xi, dim_y + 3), stroke=LINE, stroke_width=THIN))
        g.add(dwg.line((xi, dim_y), (xi + w, dim_y), stroke=LINE, stroke_width=THIN))
        _arrow(dwg, g, xi, dim_y, 0)
        _arrow(dwg, g, xi + w, dim_y, math.pi)
        _txt(dwg, g, xi + w / 2, dim_y - 1.5, f"{seg.length:g}", size=3.0)
        xi += w
    g.add(dwg.line((xi, dim_y - 3), (xi, dim_y + 3), stroke=LINE, stroke_width=THIN))
    oy = dim_y + 10
    total_len = sum(s.length for s in spec.segments)
    g.add(dwg.line((cx0, oy), (xi, oy), stroke=LINE, stroke_width=THIN))
    _arrow(dwg, g, cx0, oy, 0)
    _arrow(dwg, g, xi, oy, math.pi)
    _txt(dwg, g, (cx0 + xi) / 2, oy - 1.5, f"{total_len:g}", size=3.2)
    return x


def _render_shaft(spec: ShaftSpec, view: str = "front") -> str:
    total_len = sum(s.length for s in spec.segments)
    max_d = max(s.diameter for s in spec.segments)
    sheet = _sheet_for(spec.title, max(total_len, max_d))
    show_frame = spec.title.show_frame
    avail_w = sheet.width_mm - 2 * MARGIN - 30
    avail_h = _avail_h(sheet, show_frame, 60)
    sf, scale_label = _pick_scale(max(total_len, max_d), min(avail_w, avail_h * 2))

    dwg, g = _new_sheet(sheet, frame=show_frame)
    cx0 = 35
    cy = MARGIN + 25 + (max_d * sf) / 2
    _draw_shaft(dwg, g, spec, cx0, cy, sf, mode=view)

    if show_frame:
        dwg.add(g)
        _title_block(dwg, spec.title, scale_label, "вал", sheet)
    else:
        _scale_note(dwg, g, cx0, cy + (max_d * sf) / 2 + 32, scale_label)
        dwg.add(g)
    return dwg.tostring()


# ── Plate / flange ───────────────────────────────────────────────────────────


def _draw_plate(dwg, g, spec: PlateSpec, cx: float, cy: float, sf: float,
                  mode: str = "front") -> None:
    extent = spec.diameter if spec.shape == "circle" else max(spec.width, spec.height)

    def cm(px, py):  # center marks (dash-dot cross)
        g.add(dwg.line((px - 5, py), (px + 5, py), stroke=LINE, stroke_width=THIN,
                       stroke_dasharray="4,1,1,1"))
        g.add(dwg.line((px, py - 5), (px, py + 5), stroke=LINE, stroke_width=THIN,
                       stroke_dasharray="4,1,1,1"))

    if spec.shape == "circle":
        r = spec.diameter * sf / 2
        g.add(dwg.circle((cx, cy), r, fill="none", stroke=LINE, stroke_width=THICK))
        cm(cx, cy)
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
        dy = cy + h / 2 + 12
        g.add(dwg.line((cx - w / 2, dy), (cx + w / 2, dy), stroke=LINE, stroke_width=THIN))
        _arrow(dwg, g, cx - w / 2, dy, 0); _arrow(dwg, g, cx + w / 2, dy, math.pi)
        _txt(dwg, g, cx, dy - 1.5, f"{spec.width:g}", size=3.2)
        dx = cx + w / 2 + 12
        g.add(dwg.line((dx, cy - h / 2), (dx, cy + h / 2), stroke=LINE, stroke_width=THIN))
        _arrow(dwg, g, dx, cy - h / 2, math.pi / 2); _arrow(dwg, g, dx, cy + h / 2, -math.pi / 2)
        _txt(dwg, g, dx + 4, cy, f"{spec.height:g}", size=3.2, rot=90)

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
        lx, ly = cx - bcr * 0.71, cy + bcr * 0.71
        _txt(dwg, g, lx - 10, ly + 6, _dim_label(spec.bolt_circle_d, "", "⌀"), size=2.8)

    # side view (thickness) to the right — sectioned (hatched) on request
    sv_x = cx + (extent * sf) / 2 + 38
    th = spec.thickness * sf
    sh = (spec.diameter if spec.shape == "circle" else spec.height) * sf
    g.add(dwg.rect((sv_x, cy - sh / 2), (th, sh), fill="none", stroke=LINE, stroke_width=THICK))
    if mode in ("section", "half_section"):
        material = tdref.classify_material(spec.title.material)
        pitch = tdref.hatch_pitch_mm(material, spec.thickness)
        central_hole = next((h for h in spec.holes if abs(h.x) < 1e-6 and abs(h.y) < 1e-6), None)
        if central_hole:
            gap = central_hole.diameter * sf
            _hatch_rect(dwg, g, sv_x, cy - sh / 2, sv_x + th, cy - gap / 2, pitch)
            _hatch_rect(dwg, g, sv_x, cy + gap / 2, sv_x + th, cy + sh / 2, pitch)
            g.add(dwg.line((sv_x, cy - gap / 2), (sv_x + th, cy - gap / 2), stroke=LINE, stroke_width=THIN))
            g.add(dwg.line((sv_x, cy + gap / 2), (sv_x + th, cy + gap / 2), stroke=LINE, stroke_width=THIN))
        else:
            _hatch_rect(dwg, g, sv_x, cy - sh / 2, sv_x + th, cy + sh / 2, pitch)
    g.add(dwg.line((sv_x, cy + sh / 2 + 8), (sv_x + th, cy + sh / 2 + 8), stroke=LINE, stroke_width=THIN))
    _txt(dwg, g, sv_x + th / 2, cy + sh / 2 + 6.5, _dim_label(spec.thickness, spec.thickness_tol), size=2.8)

    if spec.roughness is not None:
        _roughness_symbol(dwg, g, sv_x - 30, cy - sh / 2 - 10, spec.roughness)


def _render_plate(spec: PlateSpec, view: str = "front") -> str:
    extent = spec.diameter if spec.shape == "circle" else max(spec.width, spec.height)
    sheet = _sheet_for(spec.title, extent)
    show_frame = spec.title.show_frame
    avail = min(sheet.width_mm - 2 * MARGIN - 40, _avail_h(sheet, show_frame, 40))
    sf, scale_label = _pick_scale(extent, avail)

    dwg, g = _new_sheet(sheet, frame=show_frame)
    cx, cy = 20 + MARGIN + (sheet.width_mm - 2 * MARGIN - 40) / 2, MARGIN + 20 + avail / 2
    _draw_plate(dwg, g, spec, cx, cy, sf, mode=view)

    if show_frame:
        dwg.add(g)
        _title_block(dwg, spec.title, scale_label, "плита", sheet)
    else:
        _scale_note(dwg, g, cx - avail / 2, cy + avail / 2 + 14, scale_label)
        dwg.add(g)
    return dwg.tostring()


# ── Assembly (сборка) ────────────────────────────────────────────────────────


def _component_footprint(comp_spec: dict) -> tuple[float, float]:
    """(width_mm, height_mm) footprint at 1:1, for assembly bounding-box layout."""
    kind = comp_spec.get("type")
    if kind == "shaft":
        s = ShaftSpec(**comp_spec)
        return (sum(seg.length for seg in s.segments), max(seg.diameter for seg in s.segments))
    if kind == "plate":
        s = PlateSpec(**comp_spec)
        extent = s.diameter if s.shape == "circle" else max(s.width, s.height)
        return (extent, extent)
    raise ValueError(f"Компонент сборки имеет неизвестный тип: {kind!r} (ожидается shaft|plate)")


_MAX_BOM_ROWS_ON_SHEET = 10


def _render_assembly(spec: AssemblySpec, view: str = "front") -> str:
    if not spec.components:
        raise ValueError("Сборка должна содержать хотя бы один компонент")

    footprints = [_component_footprint(c.spec) for c in spec.components]
    xs_min = min(c.x - fw / 2 for c, (fw, fh) in zip(spec.components, footprints))
    xs_max = max(c.x + fw / 2 for c, (fw, fh) in zip(spec.components, footprints))
    ys_min = min(c.y - fh / 2 for c, (fw, fh) in zip(spec.components, footprints))
    ys_max = max(c.y + fh / 2 for c, (fw, fh) in zip(spec.components, footprints))
    total_w, total_h = max(xs_max - xs_min, 1.0), max(ys_max - ys_min, 1.0)

    sheet = _sheet_for(spec.title, max(total_w, total_h))
    show_frame = spec.title.show_frame
    avail_w = sheet.width_mm - 2 * MARGIN - 40
    avail_h = _avail_h(sheet, show_frame, 40)
    sf, scale_label = _pick_scale(max(total_w, total_h), min(avail_w, avail_h))

    dwg, g = _new_sheet(sheet, frame=show_frame)
    cx = 20 + MARGIN + avail_w / 2
    cy = MARGIN + 20 + avail_h / 2

    for comp, (fw, fh) in zip(spec.components, footprints):
        kind = comp.spec.get("type")
        ox = cx + comp.x * sf
        oy = cy - comp.y * sf
        if kind == "shaft":
            s = ShaftSpec(**comp.spec)
            start_x = ox - (fw * sf) / 2
            _draw_shaft(dwg, g, s, start_x, oy, sf, mode=view)
        elif kind == "plate":
            p = PlateSpec(**comp.spec)
            _draw_plate(dwg, g, p, ox, oy, sf, mode=view)
        # position marker (выносная полка с номером позиции) — straight up from
        # the component's own top edge, so it doesn't cross into a neighbour's
        # dimensions/callouts.
        top_y = oy - (fh * sf) / 2
        mark_x, mark_y = ox, top_y - 30
        g.add(dwg.line((ox, top_y), (mark_x, mark_y), stroke=LINE, stroke_width=THIN))
        g.add(dwg.circle((mark_x, mark_y), 3.2, fill="white", stroke=LINE, stroke_width=THIN))
        _txt(dwg, g, mark_x, mark_y + 1, comp.ref, size=3.0)

    if show_frame:
        dwg.add(g)
        if spec.bom:
            _bom_table(dwg, spec.bom, sheet)
        _title_block(dwg, spec.title, scale_label, "сборка", sheet)
    else:
        _scale_note(dwg, g, 20 + MARGIN, cy + avail_h / 2 + 14, scale_label)
        dwg.add(g)
        if spec.bom:
            _bom_table(dwg, spec.bom, sheet)
    return dwg.tostring()


def _bom_table(dwg, rows: list[BomRow], sheet: tdref.SheetFormat) -> None:
    """Compact спецификация table, top-right corner (v1: single sheet, ≤10 rows)."""
    g = dwg.g()
    shown = rows[:_MAX_BOM_ROWS_ON_SHEET]
    x0, y0 = sheet.width_mm - 25 - 110, 8
    row_h = 5.0
    cols = (10, 25, 45, 12, 18)  # Поз | Обозначение | Наименование | Кол | Материал
    headers = ("Поз.", "Обознач.", "Наименование", "Кол.", "Материал")
    w_total = sum(cols)
    g.add(dwg.rect((x0, y0), (w_total, row_h * (len(shown) + 1)), fill="white",
                   stroke=LINE, stroke_width=THIN))
    xc = x0
    for w, h in zip(cols, headers):
        _txt(dwg, g, xc + w / 2, y0 + row_h - 1.2, h, size=2.2)
        xc += w
    for i, row in enumerate(shown):
        yy = y0 + row_h * (i + 1)
        g.add(dwg.line((x0, yy), (x0 + w_total, yy), stroke=LINE, stroke_width=0.2))
        vals = (str(row.pos), row.designation, row.name, str(row.qty), row.material)
        xc = x0
        for w, v in zip(cols, vals):
            _txt(dwg, g, xc + w / 2, yy + row_h - 1.2, (v or "")[:16], size=2.0)
            xc += w
    if len(rows) > _MAX_BOM_ROWS_ON_SHEET:
        _txt(dwg, g, x0 + w_total / 2, y0 + row_h * (len(shown) + 1) + 3,
             "см. полную спецификацию в DXF", size=2.0)
    dwg.add(g)


# ── ГОСТ title block (form 1) ─────────────────────────────────────────────────


def _title_block(dwg, tb: TitleBlock, scale_label: str, default_kind: str,
                   sheet: tdref.SheetFormat):
    x0 = sheet.width_mm - 25 - TB_W
    y0 = sheet.height_mm - 10 - TB_H
    g = dwg.g()
    g.add(dwg.rect((x0, y0), (TB_W, TB_H), fill="none", stroke=LINE, stroke_width=THICK))
    for yy in (8, 16, 24, 32, 40, 47):
        g.add(dwg.line((x0, y0 + yy), (x0 + TB_W, y0 + yy), stroke=LINE, stroke_width=THIN))
    g.add(dwg.line((x0 + 65, y0), (x0 + 65, y0 + 47), stroke=LINE, stroke_width=THIN))
    g.add(dwg.line((x0 + 135, y0), (x0 + 135, y0 + TB_H), stroke=LINE, stroke_width=THIN))

    _txt(dwg, g, x0 + 100, y0 + 14, tb.name or default_kind, size=4.2)
    _txt(dwg, g, x0 + 100, y0 + 30, tb.designation or "—", size=4.0)
    # right column: material / scale / mass
    _txt(dwg, g, x0 + 160, y0 + 6, "Материал", size=2.4)
    _txt(dwg, g, x0 + 160, y0 + 12, (tb.material or "—")[:32], size=2.3)
    _txt(dwg, g, x0 + 145, y0 + 52, "Масштаб", size=2.4, anchor="start")
    _txt(dwg, g, x0 + 168, y0 + 52, tb.scale or scale_label, size=3.4)
    # ГОСТ 2.104: Лит. / Масса / Лист / Листов — right column, below "Материал"
    # (12..47 there is otherwise empty: material ends ~y+12, scale starts y+47).
    _txt(dwg, g, x0 + 140, y0 + 20, "Лит.", size=2.0, anchor="start")
    _txt(dwg, g, x0 + 155, y0 + 20, tb.litera or "—", size=2.2, anchor="start")
    mass_label = f"{tb.mass_kg:g}" if tb.mass_kg is not None else "—"
    _txt(dwg, g, x0 + 140, y0 + 27, "Масса", size=2.0, anchor="start")
    _txt(dwg, g, x0 + 160, y0 + 27, mass_label, size=2.2, anchor="start")
    _txt(dwg, g, x0 + 140, y0 + 34, f"Лист {tb.sheet_no}", size=2.0, anchor="start")
    _txt(dwg, g, x0 + 140, y0 + 41, f"Листов {tb.sheet_count}", size=2.0, anchor="start")
    # bottom-left signatures area
    for lab, yy, val in (
        ("Разраб.", 6, tb.developer),
        ("Пров.", 14, tb.checked_by),
        ("Н.контр.", 22, tb.norm_checked_by),
        ("Утв.", 30, tb.approved_by),
    ):
        _txt(dwg, g, x0 + 2, y0 + yy, lab, size=2.4, anchor="start")
        _txt(dwg, g, x0 + 30, y0 + yy, (val or "")[:14], size=2.6, anchor="start")
    if tb.date:
        _txt(dwg, g, x0 + 55, y0 + 44, tb.date, size=2.2, anchor="start")
    _txt(dwg, g, x0 + 100, y0 + 53, tb.company, size=2.6)
    dwg.add(g)


# ── Isometric (pictorial) views ───────────────────────────────────────────────


def _render_shaft_iso(spec: ShaftSpec) -> str:
    """Pictorial 3D view of a shaft: cylinders with elliptical ends + step rings."""
    total_len = sum(s.length for s in spec.segments)
    max_d = max(s.diameter for s in spec.segments)
    sheet = _sheet_for(spec.title, max(total_len * 1.1, max_d))
    show_frame = spec.title.show_frame
    avail_w = sheet.width_mm - 2 * MARGIN - 40
    avail_h = _avail_h(sheet, show_frame, 30)
    sf, scale_label = _pick_scale(max(total_len * 1.1, max_d), min(avail_w, avail_h * 2.2))

    dwg, g = _new_sheet(sheet, frame=show_frame)

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
        g.add(dwg.line((x, top), (x + w, top), stroke=LINE, stroke_width=THICK))
        g.add(dwg.line((x, bot), (x + w, bot), stroke=LINE, stroke_width=THICK))
        prev_ry = (segs[i - 1].diameter * sf / 2) if i > 0 else 0
        if i == 0 or ry > prev_ry:
            g.add(dwg.ellipse((x, cy), (rx, ry), fill="white", stroke=LINE, stroke_width=THIN))
        else:
            g.add(dwg.path(d=f"M {x} {top} A {rx} {ry} 0 0 0 {x} {bot}",
                           fill="none", stroke=LINE, stroke_width=THIN))
        g.add(dwg.ellipse((x + w, cy), (rx, ry), fill="white", stroke=LINE, stroke_width=THICK))
        for fr in (0.55, 0.72):
            yy = cy - ry + 2 * ry * fr
            g.add(dwg.line((x + rx * 0.4, yy), (x + w, yy), stroke="#bbb", stroke_width=0.2))
        label = seg.thread or _dim_label(seg.diameter, seg.tolerance, "⌀")
        _txt(dwg, g, x + w / 2, top - 3, label, size=3.0)
        x += w

    _txt(dwg, g, (cx0 + x) / 2, cy + max_d * sf / 2 + 12,
         f"Изометрия · L={total_len:g}", size=3.0)
    if show_frame:
        dwg.add(g)
        _title_block(dwg, spec.title, scale_label, "вал", sheet)
    else:
        _scale_note(dwg, g, cx0, cy + max_d * sf / 2 + 20, scale_label)
        dwg.add(g)
    return dwg.tostring()


def _render_plate_iso(spec: PlateSpec) -> str:
    """Pictorial 3D view of a plate/flange: extruded prism/cylinder with depth."""
    extent = spec.diameter if spec.shape == "circle" else max(spec.width, spec.height)
    sheet = _sheet_for(spec.title, extent * 1.3)
    show_frame = spec.title.show_frame
    sf, scale_label = _pick_scale(extent * 1.3, sheet.width_mm - 2 * MARGIN - 60)
    dwg, g = _new_sheet(sheet, frame=show_frame)
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
    if show_frame:
        dwg.add(g)
        _title_block(dwg, spec.title, scale_label, "плита", sheet)
    else:
        _scale_note(dwg, g, cx - extent * sf * 0.5, cy + extent * sf * 0.4, scale_label)
        dwg.add(g)
    return dwg.tostring()


# ── LLM spec prompt ────────────────────────────────────────────────────────────

SPEC_SYSTEM_PROMPT = (
    "Ты — инженер-конструктор. По описанию детали верни СТРОГО JSON-спецификацию "
    "для построения точного технического чертежа (ЕСКД). Без пояснений, только JSON.\n\n"
    "Тип 'shaft' (вал/ось — ступенчатое тело вращения):\n"
    '{"type":"shaft","segments":[{"diameter":50,"length":40,"tolerance":"h6",'
    '"roughness":0.8,"chamfer":1.5,"thread":"","bore_diameter":0,"section_hatch":false}],'
    '"title":{"name":"...","designation":"","material":"Сталь 45 ГОСТ 1050-2013","developer":""}}\n'
    "Тип 'plate' (плита/фланец/крышка — плоская деталь):\n"
    '{"type":"plate","shape":"circle|rect","diameter":120,"width":100,"height":80,'
    '"thickness":14,"thickness_tol":"h12","holes":[{"x":0,"y":0,"diameter":40,"tolerance":"H7"}],'
    '"bolt_circle_d":90,"bolt_circle_n":6,"bolt_hole_d":11,"bolt_hole_tol":"H12","roughness":3.2,'
    '"title":{"name":"...","material":"...","developer":""}}\n'
    "Тип 'assembly' (сборка из готовых shaft/plate компонентов):\n"
    '{"type":"assembly","components":[{"ref":"1","spec":{"type":"shaft","segments":[...]},"x":0,"y":0},'
    '{"ref":"2","spec":{"type":"plate","shape":"circle","diameter":80,...},"x":90,"y":0}],'
    '"bom":[{"pos":1,"name":"Вал","qty":1,"material":"Сталь 45"},{"pos":2,"name":"Фланец","qty":1}],'
    '"title":{"name":"Сборка"}}\n\n'
    "Правила: размеры в мм; tolerance — квалитет/посадка (h6,k6,H7,js6,...); roughness — Ra в мкм "
    "(0.8/1.6/3.2/6.3); thread — метрическая резьба ('M20×1.5') или '' если нет; "
    "для plate координаты отверстий x,y от центра. view может быть front|isometric|section|half_section "
    "(section/half_section — разрез со штриховкой; для показа внутренней расточки задай bore_diameter). "
    "Подбирай реалистичные значения и материал по ГОСТ, если не заданы. "
    "title.show_frame по умолчанию false — рамка листа и угловой штамп НЕ рисуются, отдаётся только "
    "сам чертёж (вид, размеры, допуски, шероховатость — это и есть соответствие ЕСКД по содержанию); "
    "укажи title.show_frame=true, только если пользователь явно просит рамку/штамп/бланк. "
    "Верни ОДИН JSON-объект."
)


# ── Public API ───────────────────────────────────────────────────────────────


def render_spec_to_svg(spec: dict, view: str = "front") -> str:
    kind = spec.get("type")
    iso = view in ("isometric", "iso", "3d")
    if kind == "shaft":
        return _render_shaft_iso(ShaftSpec(**spec)) if iso else _render_shaft(ShaftSpec(**spec), view)
    if kind == "plate":
        return _render_plate_iso(PlateSpec(**spec)) if iso else _render_plate(PlateSpec(**spec), view)
    if kind == "assembly":
        if iso:
            raise ValueError("Изометрия для сборок пока не поддерживается — используйте front|section")
        return _render_assembly(AssemblySpec(**spec), view)
    raise ValueError(f"Неизвестный тип чертежа: {kind!r} (ожидается shaft|plate|assembly)")


def _autocrop_png(png_bytes: bytes, pad_px: int = 24) -> bytes:
    """Crop a rendered sheet-sized PNG down to its actual content — with
    ``show_frame=False`` there's no visible sheet border to justify keeping
    the full A4/A3/A2 canvas; the result should be just the drawing."""
    from PIL import Image, ImageChops

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    bg = Image.new("RGB", img.size, (255, 255, 255))
    bbox = ImageChops.difference(img, bg).getbbox()
    if bbox is None:
        return png_bytes
    x0, y0, x1, y1 = bbox
    x0, y0 = max(0, x0 - pad_px), max(0, y0 - pad_px)
    x1, y1 = min(img.width, x1 + pad_px), min(img.height, y1 + pad_px)
    buf = io.BytesIO()
    img.crop((x0, y0, x1, y1)).save(buf, format="PNG")
    return buf.getvalue()


def _spec_show_frame(spec: dict) -> bool:
    return bool(spec.get("title", {}).get("show_frame", False))


def render_spec_to_png(spec: dict, scale: float = 2.0, view: str = "front") -> bytes:
    import cairosvg

    svg = render_spec_to_svg(spec, view=view)
    png = cairosvg.svg2png(bytestring=svg.encode("utf-8"), scale=scale)
    if not _spec_show_frame(spec):
        png = _autocrop_png(png)
    return png


def _dxf_dim_override() -> dict:
    return {
        "dimtxt": 3.5,
        "dimasz": 2.5,
        "dimexo": 1.0,
        "dimexe": 1.5,
        "dimdli": 7.0,
        "dimclrd": 7,
        "dimclre": 7,
        "dimclrt": 7,
        "dimtad": 1,
        "dimzin": 8,
    }


def _dxf_render_dim(dim) -> None:
    try:
        dim.render()
    except Exception as exc:  # noqa: BLE001
        logger.warning("techdraw_dxf_dimension_render_failed", error=str(exc))


def _dxf_text(msp, text: str, at: tuple[float, float], height: float = 3.5,
              layer: str = "ANNOTATION", rotation: float = 0.0) -> None:
    entity = msp.add_text(text, height=height, dxfattribs={"layer": layer, "rotation": rotation})
    entity.set_placement(at)


def _dxf_roughness(msp, x: float, y: float, ra: float) -> None:
    attrs = {"layer": "ROUGHNESS"}
    msp.add_line((x, y), (x - 2.5, y + 4.3), dxfattribs=attrs)
    msp.add_line((x, y), (x + 5.0, y - 4.3), dxfattribs=attrs)
    msp.add_line((x + 5.0, y - 4.3), (x + 11.0, y - 4.3), dxfattribs=attrs)
    _dxf_text(msp, f"Ra {ra:g}", (x + 5.5, y - 8.0), height=2.8, layer="ROUGHNESS")


def _dxf_draw_shaft(msp, s: ShaftSpec, ox: float = 0.0, oy: float = 0.0) -> None:
    x = ox
    cy = oy
    prev_h = None
    max_d = max(seg.diameter for seg in s.segments)
    dim_base_y = cy - max_d / 2 - 14
    total_len = sum(seg.length for seg in s.segments)

    for seg in s.segments:
        w, h = seg.length, seg.diameter
        top, bot = cy + h / 2, cy - h / 2
        msp.add_line((x, top), (x + w, top), dxfattribs={"layer": "OBJECT"})
        msp.add_line((x, bot), (x + w, bot), dxfattribs={"layer": "OBJECT"})
        if prev_h is None or abs(prev_h - h) > 1e-9:
            yy = max((prev_h or h) / 2, h / 2)
            msp.add_line((x, cy - yy), (x, cy + yy), dxfattribs={"layer": "OBJECT"})
        if seg.bore_diameter:
            bh = seg.bore_diameter
            msp.add_line((x, cy + bh / 2), (x + w, cy + bh / 2), dxfattribs={"layer": "CENTER"})
            msp.add_line((x, cy - bh / 2), (x + w, cy - bh / 2), dxfattribs={"layer": "CENTER"})
        thread_spec = tdref.parse_thread(seg.thread) if seg.thread else None
        if thread_spec:
            minor = tdref.minor_diameter_mm(thread_spec)
            msp.add_line((x, cy + minor / 2), (x + w, cy + minor / 2), dxfattribs={"layer": "CENTER"})
            msp.add_line((x, cy - minor / 2), (x + w, cy - minor / 2), dxfattribs={"layer": "CENTER"})

        # ГОСТ-style dimensions: real DIMENSION entities, not decorative text.
        _dxf_render_dim(msp.add_linear_dim(
            base=(x, dim_base_y),
            p1=(x, bot),
            p2=(x + w, bot),
            text=f"{seg.length:g}",
            angle=0,
            override=_dxf_dim_override(),
            dxfattribs={"layer": "DIM"},
        ))
        dia_text = seg.thread or _dim_label(seg.diameter, seg.tolerance, "⌀")
        _dxf_render_dim(msp.add_linear_dim(
            base=(x + w / 2 + 6, cy),
            p1=(x + w / 2, bot),
            p2=(x + w / 2, top),
            text=dia_text,
            angle=90,
            override=_dxf_dim_override(),
            dxfattribs={"layer": "DIM"},
        ))
        if seg.roughness is not None:
            _dxf_roughness(msp, x + w / 2 + 8, top + 8, seg.roughness)
        prev_h = h
        x += w
    msp.add_line((x, cy - prev_h / 2), (x, cy + prev_h / 2), dxfattribs={"layer": "OBJECT"})
    msp.add_line((ox - 6, cy), (ox + total_len + 6, cy), dxfattribs={"layer": "CENTER"})
    _dxf_render_dim(msp.add_linear_dim(
        base=(ox, dim_base_y - 10),
        p1=(ox, cy - max_d / 2),
        p2=(ox + total_len, cy - max_d / 2),
        text=f"{total_len:g}",
        angle=0,
        override=_dxf_dim_override(),
        dxfattribs={"layer": "DIM"},
    ))


def _dxf_draw_plate(msp, s: PlateSpec, ox: float = 0.0, oy: float = 0.0) -> None:
    if s.shape == "circle":
        msp.add_circle((ox, oy), s.diameter / 2, dxfattribs={"layer": "OBJECT"})
        _dxf_render_dim(msp.add_diameter_dim(
            center=(ox, oy),
            radius=s.diameter / 2,
            angle=35,
            text=_dim_label(s.diameter, "", "⌀"),
            override=_dxf_dim_override(),
            dxfattribs={"layer": "DIM"},
        ))
        extent = s.diameter
    else:
        msp.add_lwpolyline(
            [(ox - s.width / 2, oy - s.height / 2), (ox + s.width / 2, oy - s.height / 2),
             (ox + s.width / 2, oy + s.height / 2), (ox - s.width / 2, oy + s.height / 2)],
            dxfattribs={"layer": "OBJECT"},
            close=True,
        )
        extent = max(s.width, s.height)
        _dxf_render_dim(msp.add_linear_dim(
            base=(ox - s.width / 2, oy - s.height / 2 - 12),
            p1=(ox - s.width / 2, oy - s.height / 2),
            p2=(ox + s.width / 2, oy - s.height / 2),
            text=f"{s.width:g}",
            angle=0,
            override=_dxf_dim_override(),
            dxfattribs={"layer": "DIM"},
        ))
        _dxf_render_dim(msp.add_linear_dim(
            base=(ox + s.width / 2 + 12, oy - s.height / 2),
            p1=(ox + s.width / 2, oy - s.height / 2),
            p2=(ox + s.width / 2, oy + s.height / 2),
            text=f"{s.height:g}",
            angle=90,
            override=_dxf_dim_override(),
            dxfattribs={"layer": "DIM"},
        ))
    msp.add_line((ox - extent / 2 - 5, oy), (ox + extent / 2 + 5, oy), dxfattribs={"layer": "CENTER"})
    msp.add_line((ox, oy - extent / 2 - 5), (ox, oy + extent / 2 + 5), dxfattribs={"layer": "CENTER"})
    for hole in s.holes:
        center = (ox + hole.x, oy + hole.y)
        msp.add_circle(center, hole.diameter / 2, dxfattribs={"layer": "OBJECT"})
        _dxf_render_dim(msp.add_diameter_dim(
            center=center,
            radius=hole.diameter / 2,
            angle=45,
            text=_dim_label(hole.diameter, hole.tolerance, "⌀"),
            override=_dxf_dim_override(),
            dxfattribs={"layer": "DIM"},
        ))
    if s.bolt_circle_d > 0 and s.bolt_circle_n > 0:
        msp.add_circle((ox, oy), s.bolt_circle_d / 2, dxfattribs={"layer": "CENTER"})
        for i in range(s.bolt_circle_n):
            ang = 2 * math.pi * i / s.bolt_circle_n
            msp.add_circle(
                (ox + s.bolt_circle_d / 2 * math.cos(ang), oy + s.bolt_circle_d / 2 * math.sin(ang)),
                s.bolt_hole_d / 2,
                dxfattribs={"layer": "OBJECT"},
            )
        _dxf_render_dim(msp.add_diameter_dim(
            center=(ox, oy),
            radius=s.bolt_circle_d / 2,
            angle=135,
            text=_dim_label(s.bolt_circle_d, "", "⌀"),
            override=_dxf_dim_override(),
            dxfattribs={"layer": "DIM"},
        ))
        _dxf_text(msp, f"{s.bolt_circle_n}x{_dim_label(s.bolt_hole_d, s.bolt_hole_tol, '⌀')}",
                  (ox - s.bolt_circle_d / 2, oy + s.bolt_circle_d / 2 + 8), height=3.0)
    side_x = ox + extent / 2 + 28
    side_h = s.diameter if s.shape == "circle" else s.height
    msp.add_lwpolyline(
        [(side_x, oy - side_h / 2), (side_x + s.thickness, oy - side_h / 2),
         (side_x + s.thickness, oy + side_h / 2), (side_x, oy + side_h / 2)],
        close=True,
        dxfattribs={"layer": "OBJECT"},
    )
    _dxf_render_dim(msp.add_linear_dim(
        base=(side_x, oy - side_h / 2 - 10),
        p1=(side_x, oy - side_h / 2),
        p2=(side_x + s.thickness, oy - side_h / 2),
        text=_dim_label(s.thickness, s.thickness_tol),
        angle=0,
        override=_dxf_dim_override(),
        dxfattribs={"layer": "DIM"},
    ))
    if s.roughness is not None:
        _dxf_roughness(msp, side_x - 12, oy + side_h / 2 + 8, s.roughness)


def render_spec_to_dxf(spec: dict) -> bytes:
    """Export exact geometry to DXF (CAD-editable), 1 drawing unit = 1 mm."""
    import ezdxf
    from ezdxf import units

    doc = ezdxf.new("R2010", setup=True)
    doc.units = units.MM
    doc.header["$INSUNITS"] = units.MM
    doc.header["$MEASUREMENT"] = 1
    for name, color, linetype in (
        ("OBJECT", 7, "CONTINUOUS"),
        ("CENTER", 3, "CENTER"),
        ("DIM", 2, "CONTINUOUS"),
        ("ANNOTATION", 7, "CONTINUOUS"),
        ("ROUGHNESS", 1, "CONTINUOUS"),
    ):
        if name not in doc.layers:
            doc.layers.add(name, color=color, linetype=linetype)
    msp = doc.modelspace()
    kind = spec.get("type")
    if kind == "shaft":
        _dxf_draw_shaft(msp, ShaftSpec(**spec))
    elif kind == "plate":
        _dxf_draw_plate(msp, PlateSpec(**spec))
    elif kind == "assembly":
        a = AssemblySpec(**spec)
        for comp in a.components:
            ckind = comp.spec.get("type")
            if ckind == "shaft":
                cs = ShaftSpec(**comp.spec)
                total_len = sum(seg.length for seg in cs.segments)
                _dxf_draw_shaft(msp, cs, ox=comp.x - total_len / 2, oy=comp.y)
            elif ckind == "plate":
                cp = PlateSpec(**comp.spec)
                _dxf_draw_plate(msp, cp, ox=comp.x, oy=comp.y)
                extent = cp.diameter if cp.shape == "circle" else max(cp.width, cp.height)
                hatch = msp.add_hatch(color=1)
                hatch.set_pattern_fill("ANSI31", scale=extent / 40 or 1.0)
                half = extent / 2
                if cp.shape == "circle":
                    hatch.paths.add_edge_path().add_arc(
                        center=(comp.x, comp.y), radius=half, start_angle=0, end_angle=360
                    )
                else:
                    hatch.paths.add_polyline_path(
                        [(comp.x - half, comp.y - half), (comp.x + half, comp.y - half),
                         (comp.x + half, comp.y + half), (comp.x - half, comp.y + half)],
                        is_closed=True,
                    )
    else:
        raise ValueError(f"Неизвестный тип чертежа: {kind!r} (ожидается shaft|plate|assembly)")
    buf = io.StringIO()
    doc.write(buf)
    return buf.getvalue().encode("utf-8")
