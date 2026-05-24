#!/usr/bin/env python3
"""Download and generate a public engineering drawing test dataset.

Sources:
  - Real downloads: Raspberry Pi mechanical PDFs, Wikimedia Commons thread SVG,
    GitHub MechanicalBlueprints DXF files.
  - Synthetic: High-quality ГОСТ-style drawings generated with PIL for each type
    (detail, assembly, section, weld) with proper annotations and ground truth.

Output:
  example-drawings/          ← drawings in multiple formats
  example-drawings/ground_truth.json  ← annotations for eval

Usage:
  python scripts/download_public_drawings.py [--output-dir PATH] [--skip-download]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# ── Config ─────────────────────────────────────────────────────────────────────

REAL_DRAWINGS: list[dict] = [
    {
        "url": "https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-mechanical-drawing.pdf",
        "filename": "rpi4_assembly_drawing.pdf",
        "drawing_type": "assembly",
        "description": "Raspberry Pi 4 PCB assembly drawing with dimensions (mm)",
        "features": [
            {"feature_type": "surface", "name": "PCB Board 85×56mm", "dimensions": [{"dim_type": "linear", "nominal": 85.0}, {"dim_type": "linear", "nominal": 56.0}]},
            {"feature_type": "hole", "name": "Mounting holes Ø2.7mm", "dimensions": [{"dim_type": "diameter", "nominal": 2.7}]},
            {"feature_type": "boss", "name": "USB-A connector", "dimensions": []},
        ],
    },
    {
        "url": "https://upload.wikimedia.org/wikipedia/commons/4/4b/ISO_and_UTS_Thread_Dimensions.svg",
        "filename": "iso_thread_dimensions.svg",
        "drawing_type": "detail",
        "description": "ISO/UTS thread profile with dimension annotations",
        "features": [
            {"feature_type": "thread", "name": "ISO thread profile H", "dimensions": [{"dim_type": "linear", "nominal": 0.0}]},
            {"feature_type": "chamfer", "name": "Thread root radius", "dimensions": []},
        ],
    },
]

# ── Synthetic drawing generation ───────────────────────────────────────────────


def _font():
    """Return a PIL ImageFont — default bitmap if truetype unavailable."""
    from PIL import ImageFont
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except Exception:
        return ImageFont.load_default()


def _font_sm():
    from PIL import ImageFont
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except Exception:
        return ImageFont.load_default()


def _font_bold():
    from PIL import ImageFont
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
    except Exception:
        return ImageFont.load_default()


def make_shaft_detail_png() -> bytes:
    """Деталь: ступенчатый вал с Ø50h6, Ø30k6, отверстием Ø10H7, шпоночным пазом, Ra, GD&T."""
    from PIL import Image, ImageDraw
    W, H = 1200, 800
    img = Image.new("RGB", (W, H), (248, 248, 248))
    d = ImageDraw.Draw(img)
    f, fsm, fb = _font(), _font_sm(), _font_bold()

    # ── Title block (bottom right, ГОСТ) ──
    d.rectangle([700, 650, W - 5, H - 5], fill=(235, 235, 235), outline=(0, 0, 0), width=1)
    d.line([(700, 650), (700, H - 5)], fill=(0, 0, 0), width=2)
    d.text((710, 658), "Вал ступенчатый", font=fb, fill=(0, 0, 0))
    d.text((710, 678), "ДП-001-01", font=f, fill=(0, 0, 0))
    d.text((710, 698), "Сталь 45 ГОСТ 1050-2013", font=f, fill=(0, 0, 0))
    d.text((710, 718), "Масштаб 1:2", font=f, fill=(0, 0, 0))
    d.text((710, 738), "Масса: 3.2 кг", font=f, fill=(0, 0, 0))
    d.text((900, 658), "Лист 1/1", font=f, fill=(0, 0, 0))

    # ── Main view: stepped shaft ──
    # Left journal Ø30
    d.rectangle([60, 300, 280, 450], outline=(0, 0, 0), width=2, fill=(255, 255, 255))
    # Shaft body Ø50
    d.rectangle([280, 250, 680, 500], outline=(0, 0, 0), width=2, fill=(255, 255, 255))
    # Right journal Ø30
    d.rectangle([680, 300, 900, 450], outline=(0, 0, 0), width=2, fill=(255, 255, 255))

    # Centre line (dash-dot)
    for x in range(30, W - 200, 20):
        d.line([(x, 375), (x + 12, 375)], fill=(0, 0, 180), width=1)

    # ── Key slot (top of shaft body) ──
    d.rectangle([350, 248, 510, 270], outline=(0, 0, 0), width=2, fill=(230, 230, 230))
    d.text((360, 230), "Паз 12×6 ГОСТ 23360", font=fsm, fill=(0, 0, 0))

    # ── Cross hole Ø10H7 ──
    d.ellipse([565, 330, 615, 420], outline=(0, 0, 0), width=2, fill=(255, 255, 255))
    d.line([(590, 310), (590, 440)], fill=(0, 0, 180), width=1)  # centre vertical
    d.text((620, 355), "Ø10H7", font=f, fill=(0, 0, 0))

    # ── Dimensions ──
    # Overall length
    d.line([(60, 540), (900, 540)], fill=(0, 0, 0), width=1)
    d.line([(60, 500), (60, 550)], fill=(0, 0, 0), width=1)
    d.line([(900, 450), (900, 550)], fill=(0, 0, 0), width=1)
    d.text((420, 545), "840", font=f, fill=(0, 0, 0))

    # Left section Ø30h6
    d.line([(60, 570), (280, 570)], fill=(0, 0, 0), width=1)
    d.text((130, 575), "220", font=fsm, fill=(0, 0, 0))
    # Diameter arrow Ø30h6
    d.line([(0, 375), (60, 375)], fill=(0, 0, 0), width=1)
    d.text((5, 380), "Ø30h6", font=f, fill=(0, 0, 0))

    # Body Ø50h6
    d.line([(0, 290), (50, 290)], fill=(0, 0, 0), width=1)
    d.text((5, 260), "Ø50h6", font=fb, fill=(0, 0, 0))

    # Right section
    d.line([(0, 420), (50, 420)], fill=(0, 0, 0), width=1)
    d.text((5, 425), "Ø30k6", font=f, fill=(0, 0, 0))

    # ── Surface roughness ──
    d.text((100, 275), "Ra 3.2", font=fsm, fill=(80, 80, 80))
    d.text((400, 225), "Ra 1.6", font=fsm, fill=(80, 80, 80))
    d.text((750, 275), "Ra 3.2", font=fsm, fill=(80, 80, 80))
    d.text((620, 220), "Ra 0.8", font=fsm, fill=(80, 80, 80))  # keyway

    # ── GD&T ──
    d.rectangle([80, 590, 230, 615], outline=(0, 0, 0), width=1)
    d.line([(140, 590), (140, 615)], fill=(0, 0, 0), width=1)
    d.text((83, 594), "⊘", font=f, fill=(0, 0, 0))
    d.text((145, 594), "0.02 A", font=f, fill=(0, 0, 0))

    d.rectangle([300, 590, 450, 615], outline=(0, 0, 0), width=1)
    d.line([(360, 590), (360, 615)], fill=(0, 0, 0), width=1)
    d.text((303, 594), "⊥", font=f, fill=(0, 0, 0))
    d.text((365, 594), "0.03 A", font=f, fill=(0, 0, 0))

    # ── Technical requirements ──
    d.text((60, 640), "ТТ:", font=fb, fill=(0, 0, 0))
    d.text((60, 658), "1. HRC 42...48 (шейки Ø30h6, Ø30k6)", font=fsm, fill=(0, 0, 0))
    d.text((60, 672), "2. Остальные Ra 6.3", font=fsm, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(200, 200))
    return buf.getvalue()


def make_flange_detail_png() -> bytes:
    """Деталь: фланец с отверстиями под болты, Ra, посадки."""
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (248, 248, 248))
    d = ImageDraw.Draw(img)
    f, fsm, fb = _font(), _font_sm(), _font_bold()

    cx, cy = 450, 380

    # Title block
    d.rectangle([580, 720, W - 5, H - 5], fill=(235, 235, 235), outline=(0, 0, 0), width=1)
    d.text((590, 728), "Фланец", font=fb, fill=(0, 0, 0))
    d.text((590, 748), "ДП-002-01", font=f, fill=(0, 0, 0))
    d.text((590, 768), "Чугун СЧ20 ГОСТ 1412", font=f, fill=(0, 0, 0))
    d.text((590, 788), "Масштаб 1:1", font=f, fill=(0, 0, 0))

    # Main view (front): flange circle
    d.ellipse([cx - 280, cy - 280, cx + 280, cy + 280], outline=(0, 0, 0), width=3)
    # Bolt circle PCD=200
    d.ellipse([cx - 100, cy - 100, cx + 100, cy + 100], outline=(0, 0, 180), width=1)
    # Inner bore Ø80H7
    d.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], outline=(0, 0, 0), width=2)
    # Centre lines
    d.line([(cx - 320, cy), (cx + 320, cy)], fill=(0, 0, 180), width=1)
    d.line([(cx, cy - 320), (cx, cy + 320)], fill=(0, 0, 180), width=1)

    # 4× bolt holes Ø18 at PCD=200
    import math
    for angle in [0, 90, 180, 270]:
        rad = math.radians(angle)
        hx = cx + int(100 * math.cos(rad))
        hy = cy + int(100 * math.sin(rad))
        d.ellipse([hx - 13, hy - 13, hx + 13, hy + 13], outline=(0, 0, 0), width=2)

    # Dimensions
    # Outer diameter
    d.line([(cx - 280, cy - 320), (cx + 280, cy - 320)], fill=(0, 0, 0), width=1)
    d.line([(cx - 280, cy - 280), (cx - 280, cy - 330)], fill=(0, 0, 0), width=1)
    d.line([(cx + 280, cy - 280), (cx + 280, cy - 330)], fill=(0, 0, 0), width=1)
    d.text((cx - 30, cy - 345), "Ø560", font=f, fill=(0, 0, 0))

    # Bore
    d.text((cx + 50, cy - 10), "Ø80H7", font=fb, fill=(0, 0, 0))

    # Bolt holes
    d.text((cx + 110, cy - 80), "4×Ø18", font=f, fill=(0, 0, 0))

    # PCD
    d.text((cx - 30, cy - 130), "PCD 200", font=fsm, fill=(60, 60, 60))

    # Roughness
    d.text((cx + 290, cy - 10), "Ra 3.2", font=fsm, fill=(80, 80, 80))
    d.text((cx + 45, cy + 45), "Ra 1.6", font=fsm, fill=(80, 80, 80))

    # Side view (right): flange profile
    sx = 800
    d.rectangle([sx, cy - 30, sx + 60, cy + 30], outline=(0, 0, 0), width=2)  # hub
    d.rectangle([sx - 30, cy - 280, sx + 90, cy + 280], outline=(0, 0, 0), width=2)  # flange body
    # Thickness dimension
    d.text((sx + 100, cy - 10), "20±0.1", font=f, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(200, 200))
    return buf.getvalue()


def make_assembly_drawing_png() -> bytes:
    """Сборка: редуктор с BOM/спецификацией и позиционными номерами."""
    from PIL import Image, ImageDraw
    W, H = 1400, 900
    img = Image.new("RGB", (W, H), (248, 248, 248))
    d = ImageDraw.Draw(img)
    f, fsm, fb = _font(), _font_sm(), _font_bold()

    # Title block
    d.rectangle([850, 720, W - 5, H - 5], fill=(235, 235, 235), outline=(0, 0, 0), width=1)
    d.text((860, 728), "Редуктор цилиндрический", font=fb, fill=(0, 0, 0))
    d.text((860, 748), "СБ-РЦ-001", font=f, fill=(0, 0, 0))
    d.text((860, 768), "Масштаб 1:2", font=f, fill=(0, 0, 0))

    # BOM table (upper right, GOST 2.106)
    bom_x, bom_y = 900, 20
    bom_items = [
        ("1", "Корпус", "1", "СЧ20"),
        ("2", "Крышка", "1", "СЧ20"),
        ("3", "Вал ведущий", "1", "Сталь 45"),
        ("4", "Вал ведомый", "1", "Сталь 45"),
        ("5", "Шестерня z=18", "1", "Сталь 40Х"),
        ("6", "Колесо z=72", "1", "Сталь 40Х"),
        ("7", "Подшипник 207", "4", "ГОСТ 8338"),
        ("8", "Болт М10×35", "8", "Сталь 10.9"),
    ]
    d.rectangle([bom_x, bom_y, W - 5, bom_y + 30 + len(bom_items) * 22], outline=(0, 0, 0), width=1)
    d.text((bom_x + 5, bom_y + 5), "СПЕЦИФИКАЦИЯ", font=fb, fill=(0, 0, 0))
    d.line([(bom_x, bom_y + 25), (W - 5, bom_y + 25)], fill=(0, 0, 0), width=1)
    cols = [bom_x + 5, bom_x + 35, bom_x + 200, bom_x + 260, bom_x + 350]
    headers = ["№", "Наименование", "Кол.", "Материал"]
    for i, h in enumerate(headers):
        d.text((cols[i], bom_y + 8), h, font=fsm, fill=(60, 60, 60))
    for row_i, (no, name, qty, mat) in enumerate(bom_items):
        y = bom_y + 30 + row_i * 22
        d.line([(bom_x, y), (W - 5, y)], fill=(180, 180, 180), width=1)
        d.text((cols[0], y + 4), no, font=f, fill=(0, 0, 0))
        d.text((cols[1], y + 4), name, font=f, fill=(0, 0, 0))
        d.text((cols[2], y + 4), qty, font=f, fill=(0, 0, 0))
        d.text((cols[3], y + 4), mat, font=f, fill=(0, 0, 0))

    # Main assembly view
    # Casing outline
    d.rectangle([60, 200, 750, 600], outline=(0, 0, 0), width=3, fill=(255, 255, 255))
    # Input shaft
    d.rectangle([0, 330, 160, 370], outline=(0, 0, 0), width=2, fill=(240, 240, 240))
    # Output shaft
    d.rectangle([650, 340, 830, 380], outline=(0, 0, 0), width=2, fill=(240, 240, 240))
    # Pinion Ø72
    d.ellipse([150, 290, 294, 410], outline=(0, 0, 0), width=2, fill=(250, 250, 240))
    # Gear Ø288
    d.ellipse([250, 200, 682, 600], outline=(0, 0, 0), width=2, fill=(250, 250, 240))
    # Centre line
    d.line([(0, 350), (850, 350)], fill=(0, 0, 180), width=1)
    d.line([(400, 150), (400, 650)], fill=(0, 0, 180), width=1)

    # Balloon callouts
    balloon_data = [
        (120, 290, "1"),   # Корпус
        (400, 185, "2"),   # Крышка
        (80, 350, "3"),    # Вал ведущий
        (720, 360, "4"),   # Вал ведомый
        (222, 340, "5"),   # Шестерня
        (465, 395, "6"),   # Колесо
        (155, 320, "7"),   # Подшипник
        (60, 440, "8"),    # Болт
    ]
    for (bx, by, num) in balloon_data:
        d.ellipse([bx - 14, by - 14, bx + 14, by + 14], outline=(0, 0, 0), width=2, fill=(255, 255, 200))
        d.text((bx - 5 if len(num) == 1 else bx - 8, by - 8), num, font=fb, fill=(0, 0, 0))

    # Key dimensions
    d.text((380, 615), "i = 4 (z₂/z₁ = 72/18)", font=f, fill=(0, 0, 0))
    d.text((60, 615), "Межосевое расстояние a = 225±0.035", font=f, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(200, 200))
    return buf.getvalue()


def make_section_view_png() -> bytes:
    """Разрез: корпус подшипника с внутренними полостями, Ra, посадки."""
    from PIL import Image, ImageDraw
    W, H = 1100, 850
    img = Image.new("RGB", (W, H), (248, 248, 248))
    d = ImageDraw.Draw(img)
    f, fsm, fb = _font(), _font_sm(), _font_bold()

    # Title block
    d.rectangle([650, 720, W - 5, H - 5], fill=(235, 235, 235), outline=(0, 0, 0), width=1)
    d.text((660, 728), "Корпус подшипника", font=fb, fill=(0, 0, 0))
    d.text((660, 748), "Разрез А-А", font=f, fill=(0, 0, 0))
    d.text((660, 768), "ДП-003-01", font=f, fill=(0, 0, 0))
    d.text((660, 788), "Алюминий АД31 ГОСТ 4784", font=f, fill=(0, 0, 0))

    # Section cutting plane label
    d.text((40, 80), "А", font=fb, fill=(0, 0, 0))
    d.text((40, 650), "А", font=fb, fill=(0, 0, 0))
    d.line([(60, 100), (60, 640)], fill=(0, 0, 0), width=2)
    for y in range(100, 640, 25):
        d.line([(55, y), (65, y + 10)], fill=(0, 0, 0), width=1)

    cx = 400

    # Outer casing
    d.rectangle([cx - 220, 150, cx + 220, 650], outline=(0, 0, 0), width=3, fill=(255, 255, 255))
    # Bore Ø120H7 (hatch pattern = inner wall)
    d.ellipse([cx - 60, 280, cx + 60, 520], outline=(0, 0, 0), width=2, fill=(255, 255, 255))

    # Hatching (section material visualization)
    for x_off in range(-220, -60, 12):
        d.line([(cx + x_off, 150), (cx + x_off - 30, 180)], fill=(100, 100, 100), width=1)
    for x_off in range(60, 220, 12):
        d.line([(cx + x_off, 150), (cx + x_off - 30, 180)], fill=(100, 100, 100), width=1)

    # Sealing groove
    d.rectangle([cx - 220, 580, cx - 185, 650], outline=(0, 0, 0), width=1, fill=(230, 230, 230))
    d.rectangle([cx + 185, 580, cx + 220, 650], outline=(0, 0, 0), width=1, fill=(230, 230, 230))
    d.text((cx - 340, 610), "Канавка\nуплотнения\n6×4", font=fsm, fill=(0, 0, 0))

    # Centre lines
    d.line([(cx - 280, 400), (cx + 280, 400)], fill=(0, 0, 180), width=1)
    d.line([(cx, 120), (cx, 680)], fill=(0, 0, 180), width=1)

    # Dimensions
    d.text((cx + 230, 390), "Ø120H7", font=fb, fill=(0, 0, 0))
    d.text((cx + 230, 270), "Ø200", font=f, fill=(0, 0, 0))
    # Wall thickness
    d.line([(cx + 60, 400), (cx + 220, 400)], fill=(0, 0, 0), width=1)
    d.text((cx + 100, 380), "80", font=fsm, fill=(0, 0, 0))
    # Height
    d.line([(cx + 250, 150), (cx + 250, 650)], fill=(0, 0, 0), width=1)
    d.line([(cx + 220, 150), (cx + 260, 150)], fill=(0, 0, 0), width=1)
    d.line([(cx + 220, 650), (cx + 260, 650)], fill=(0, 0, 0), width=1)
    d.text((cx + 260, 390), "500±0.2", font=f, fill=(0, 0, 0))

    # Roughness
    d.text((cx + 65, 290), "Ra 1.6", font=fsm, fill=(80, 80, 80))
    d.text((cx - 220, 165), "Ra 6.3", font=fsm, fill=(80, 80, 80))
    d.text((cx - 180, 590), "Ra 3.2", font=fsm, fill=(80, 80, 80))

    # Internal pocket
    d.rectangle([cx - 150, 350, cx - 80, 450], outline=(0, 0, 0), width=2, fill=(235, 235, 235))
    d.text((cx - 220, 340), "Карман\n50×40×20", font=fsm, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(200, 200))
    return buf.getvalue()


def make_weld_drawing_png() -> bytes:
    """Сварная конструкция: кронштейн с обозначениями швов по ГОСТ 2.312."""
    from PIL import Image, ImageDraw
    W, H = 1100, 800
    img = Image.new("RGB", (W, H), (248, 248, 248))
    d = ImageDraw.Draw(img)
    f, fsm, fb = _font(), _font_sm(), _font_bold()

    # Title block
    d.rectangle([650, 680, W - 5, H - 5], fill=(235, 235, 235), outline=(0, 0, 0), width=1)
    d.text((660, 688), "Кронштейн сварной", font=fb, fill=(0, 0, 0))
    d.text((660, 708), "ДП-004-01", font=f, fill=(0, 0, 0))
    d.text((660, 728), "Ст3сп ГОСТ 380", font=f, fill=(0, 0, 0))
    d.text((660, 748), "Масштаб 1:2", font=f, fill=(0, 0, 0))

    # Base plate
    d.rectangle([100, 500, 700, 560], outline=(0, 0, 0), width=3, fill=(240, 240, 240))
    # Vertical rib
    d.rectangle([280, 200, 360, 500], outline=(0, 0, 0), width=3, fill=(240, 240, 240))
    # Top plate
    d.rectangle([100, 180, 500, 230], outline=(0, 0, 0), width=3, fill=(240, 240, 240))
    # Triangular gusset (approximated as polygon)
    d.polygon([(360, 500), (360, 350), (500, 500)], outline=(0, 0, 0), fill=(235, 235, 235))
    d.line([(360, 500), (360, 350)], fill=(0, 0, 0), width=3)
    d.line([(360, 350), (500, 500)], fill=(0, 0, 0), width=3)
    d.line([(500, 500), (360, 500)], fill=(0, 0, 0), width=3)

    # Mounting holes in base plate
    for hx in [160, 260, 560, 660]:
        d.ellipse([hx - 12, 514, hx + 12, 546], outline=(0, 0, 0), width=2)
    d.text((350, 562), "4×Ø18", font=fsm, fill=(0, 0, 0))

    # Weld symbols (ГОСТ 2.312)
    # Horizontal weld: base ↔ rib
    d.line([(280, 510), (260, 550)], fill=(0, 0, 0), width=1)
    d.line([(260, 550), (200, 550)], fill=(0, 0, 0), width=2)
    d.text((160, 552), "▽5", font=f, fill=(0, 0, 0))
    d.text((100, 570), "Шов Т1 ГОСТ 5264-80", font=fsm, fill=(60, 60, 60))

    # Weld: rib ↔ top plate
    d.line([(320, 230), (300, 280)], fill=(0, 0, 0), width=1)
    d.line([(300, 280), (200, 280)], fill=(0, 0, 0), width=2)
    d.text((100, 275), "▽6 ГОСТ 14771", font=f, fill=(0, 0, 0))

    # Weld: gusset
    d.line([(440, 430), (520, 390)], fill=(0, 0, 0), width=1)
    d.line([(520, 390), (600, 390)], fill=(0, 0, 0), width=2)
    d.text((610, 388), "▽5 (4)", font=f, fill=(0, 0, 0))

    # Dimensions
    d.line([(100, 620), (700, 620)], fill=(0, 0, 0), width=1)
    d.line([(100, 560), (100, 630)], fill=(0, 0, 0), width=1)
    d.line([(700, 560), (700, 630)], fill=(0, 0, 0), width=1)
    d.text((370, 625), "600", font=f, fill=(0, 0, 0))

    d.line([(720, 180), (720, 560)], fill=(0, 0, 0), width=1)
    d.line([(700, 180), (730, 180)], fill=(0, 0, 0), width=1)
    d.line([(700, 560), (730, 560)], fill=(0, 0, 0), width=1)
    d.text((725, 360), "380", font=f, fill=(0, 0, 0))

    # Technical requirements
    d.text((100, 640), "ТТ: 1. Сварка МИГ. Электрод ER70S-6. 2. Зачистить брызги. 3. Грунтовать ГФ-021.", font=fsm, fill=(0, 0, 0))

    buf = io.BytesIO()
    img.save(buf, format="PNG", dpi=(200, 200))
    return buf.getvalue()


def make_gear_detail_dxf() -> bytes:
    """Деталь: шестерня — DXF с размерами и посадками."""
    try:
        import ezdxf
        import tempfile, os
    except ImportError:
        return b""

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    msp.add_circle((0, 0), radius=50)
    c = msp.add_circle((0, 0), radius=40)
    c.dxf.linetype = "CENTER"
    msp.add_circle((0, 0), radius=10)
    msp.add_lwpolyline([(-4, 10), (4, 10), (4, 15), (-4, 15), (-4, 10)], close=True)
    msp.add_line((-60, 0), (60, 0))
    msp.add_line((0, -60), (0, 60))
    msp.add_text("Ø100", dxfattribs={"insert": (-15, -70), "height": 4})
    msp.add_text("Ø20H7", dxfattribs={"insert": (-15, -80), "height": 4})

    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        doc.saveas(tmp_path)
        data = Path(tmp_path).read_bytes()
    finally:
        os.unlink(tmp_path)
    return data


def make_threaded_shaft_dxf() -> bytes:
    """Деталь с резьбой: болт М20×1.5-6g — DXF."""
    try:
        import ezdxf
        import tempfile, os
    except ImportError:
        return b""

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()

    msp.add_lwpolyline([
        (0, -10), (80, -10), (80, -8), (100, -8),
        (100, 8), (80, 8), (80, 10), (0, 10), (0, -10),
    ], close=True)
    msp.add_line((80, -8), (100, -8))
    msp.add_line((80, 8), (100, 8))
    msp.add_line((-10, 0), (120, 0))
    msp.add_circle((0, 0), radius=17)
    msp.add_text("L=100", dxfattribs={"insert": (30, 15), "height": 4})
    msp.add_text("М20×1.5-6g", dxfattribs={"insert": (75, 15), "height": 4})

    with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        doc.saveas(tmp_path)
        data = Path(tmp_path).read_bytes()
    finally:
        os.unlink(tmp_path)
    return data


# ── Synthetic dataset definition ────────────────────────────────────────────────

SYNTHETIC_DRAWINGS: list[dict] = [
    {
        "filename": "shaft_detail.png",
        "drawing_type": "detail",
        "generator": make_shaft_detail_png,
        "description": "Ступенчатый вал Ø50/Ø30, шпоночный паз, отверстие Ø10H7, Ra, GD&T",
        "features": [
            {"feature_type": "surface", "name": "Шейка Ø50h6",
             "dimensions": [{"dim_type": "diameter", "nominal": 50.0, "fit_system": "h6"}],
             "surfaces": [{"roughness_type": "Ra", "value": 0.8}]},
            {"feature_type": "surface", "name": "Шейка Ø30h6",
             "dimensions": [{"dim_type": "diameter", "nominal": 30.0, "fit_system": "h6"}],
             "surfaces": [{"roughness_type": "Ra", "value": 3.2}]},
            {"feature_type": "surface", "name": "Шейка Ø30k6",
             "dimensions": [{"dim_type": "diameter", "nominal": 30.0, "fit_system": "k6"}],
             "surfaces": [{"roughness_type": "Ra", "value": 3.2}]},
            {"feature_type": "hole", "name": "Отверстие Ø10H7",
             "dimensions": [{"dim_type": "diameter", "nominal": 10.0, "fit_system": "H7"}],
             "surfaces": [{"roughness_type": "Ra", "value": 1.6}]},
            {"feature_type": "key_slot", "name": "Шпоночный паз 12×6",
             "dimensions": [{"dim_type": "linear", "nominal": 12.0}, {"dim_type": "depth", "nominal": 6.0}],
             "surfaces": [{"roughness_type": "Ra", "value": 1.6}]},
            {"feature_type": "surface", "name": "Длина вала L=840",
             "dimensions": [{"dim_type": "linear", "nominal": 840.0}], "surfaces": []},
        ],
        "gdt": [
            {"symbol": "cylindricity", "tolerance_value": 0.02, "datum_reference": "A"},
            {"symbol": "perpendicularity", "tolerance_value": 0.03, "datum_reference": "A"},
        ],
    },
    {
        "filename": "flange_detail.png",
        "drawing_type": "detail",
        "generator": make_flange_detail_png,
        "description": "Фланец Ø560 с 4×Ø18 отверстиями под болты, центральное отверстие Ø80H7",
        "features": [
            {"feature_type": "surface", "name": "Наружный диаметр Ø560",
             "dimensions": [{"dim_type": "diameter", "nominal": 560.0}],
             "surfaces": [{"roughness_type": "Ra", "value": 3.2}]},
            {"feature_type": "hole", "name": "Центральное отверстие Ø80H7",
             "dimensions": [{"dim_type": "diameter", "nominal": 80.0, "fit_system": "H7"}],
             "surfaces": [{"roughness_type": "Ra", "value": 1.6}]},
            {"feature_type": "hole", "name": "Болтовые отверстия 4×Ø18",
             "dimensions": [{"dim_type": "diameter", "nominal": 18.0}], "surfaces": []},
            {"feature_type": "surface", "name": "Торцевая поверхность t=20±0.1",
             "dimensions": [{"dim_type": "linear", "nominal": 20.0, "upper_tol": 0.1, "lower_tol": -0.1}],
             "surfaces": [{"roughness_type": "Ra", "value": 3.2}]},
        ],
        "gdt": [],
    },
    {
        "filename": "gearbox_assembly.png",
        "drawing_type": "assembly",
        "generator": make_assembly_drawing_png,
        "description": "Одноступенчатый цилиндрический редуктор i=4, спецификация 8 позиций",
        "features": [
            {"feature_type": "other", "name": "Корпус", "dimensions": [], "surfaces": []},
            {"feature_type": "other", "name": "Крышка", "dimensions": [], "surfaces": []},
            {"feature_type": "other", "name": "Вал ведущий", "dimensions": [], "surfaces": []},
            {"feature_type": "other", "name": "Вал ведомый", "dimensions": [], "surfaces": []},
            {"feature_type": "other", "name": "Шестерня z=18", "dimensions": [], "surfaces": []},
            {"feature_type": "other", "name": "Колесо z=72", "dimensions": [], "surfaces": []},
            {"feature_type": "other", "name": "Подшипник 207", "dimensions": [], "surfaces": []},
            {"feature_type": "other", "name": "Болт М10×35", "dimensions": [], "surfaces": []},
        ],
        "gdt": [],
    },
    {
        "filename": "bearing_housing_section.png",
        "drawing_type": "detail",
        "generator": make_section_view_png,
        "description": "Разрез А-А корпуса подшипника: расточка Ø120H7, карман, канавки уплотнения",
        "features": [
            {"feature_type": "hole", "name": "Расточка Ø120H7",
             "dimensions": [{"dim_type": "diameter", "nominal": 120.0, "fit_system": "H7"}],
             "surfaces": [{"roughness_type": "Ra", "value": 1.6}]},
            {"feature_type": "surface", "name": "Наружный диаметр Ø200",
             "dimensions": [{"dim_type": "diameter", "nominal": 200.0}],
             "surfaces": [{"roughness_type": "Ra", "value": 6.3}]},
            {"feature_type": "pocket", "name": "Карман 50×40×20",
             "dimensions": [{"dim_type": "linear", "nominal": 50.0}],
             "surfaces": [{"roughness_type": "Ra", "value": 3.2}]},
            {"feature_type": "groove", "name": "Канавка уплотнения 6×4",
             "dimensions": [{"dim_type": "linear", "nominal": 6.0}, {"dim_type": "depth", "nominal": 4.0}],
             "surfaces": []},
            {"feature_type": "surface", "name": "Высота корпуса L=500±0.2",
             "dimensions": [{"dim_type": "linear", "nominal": 500.0, "upper_tol": 0.2, "lower_tol": -0.2}],
             "surfaces": []},
        ],
        "gdt": [],
    },
    {
        "filename": "welded_bracket.png",
        "drawing_type": "detail",
        "generator": make_weld_drawing_png,
        "description": "Кронштейн сварной Ст3сп: основание, ребро, косынка, швы по ГОСТ 5264 и 14771",
        "features": [
            {"feature_type": "surface", "name": "Основание L=600",
             "dimensions": [{"dim_type": "linear", "nominal": 600.0}], "surfaces": []},
            {"feature_type": "hole", "name": "Крепёжные отверстия 4×Ø18",
             "dimensions": [{"dim_type": "diameter", "nominal": 18.0}], "surfaces": []},
            {"feature_type": "weld", "name": "Шов Т1 катет 5мм ГОСТ 5264",
             "dimensions": [{"dim_type": "linear", "nominal": 5.0}], "surfaces": []},
            {"feature_type": "weld", "name": "Шов катет 6мм ГОСТ 14771",
             "dimensions": [{"dim_type": "linear", "nominal": 6.0}], "surfaces": []},
            {"feature_type": "weld", "name": "Шов косынки катет 5мм (4 шва)",
             "dimensions": [{"dim_type": "linear", "nominal": 5.0}], "surfaces": []},
        ],
        "gdt": [],
    },
    {
        "filename": "gear_detail.dxf",
        "drawing_type": "detail",
        "generator": make_gear_detail_dxf,
        "description": "Шестерня Ø100 с отверстием Ø20H7 и шпоночным пазом — DXF",
        "features": [
            {"feature_type": "surface", "name": "Наружный диаметр Ø100",
             "dimensions": [{"dim_type": "diameter", "nominal": 100.0}], "surfaces": []},
            {"feature_type": "hole", "name": "Отверстие Ø20H7",
             "dimensions": [{"dim_type": "diameter", "nominal": 20.0, "fit_system": "H7"}],
             "surfaces": []},
            {"feature_type": "key_slot", "name": "Шпоночный паз",
             "dimensions": [{"dim_type": "linear", "nominal": 8.0}], "surfaces": []},
        ],
        "gdt": [],
    },
    {
        "filename": "threaded_shaft.dxf",
        "drawing_type": "detail",
        "generator": make_threaded_shaft_dxf,
        "description": "Болт с резьбой М20×1.5-6g — DXF",
        "features": [
            {"feature_type": "thread", "name": "Резьба М20×1.5-6g",
             "dimensions": [{"dim_type": "diameter", "nominal": 20.0, "fit_system": "6g"}],
             "surfaces": []},
            {"feature_type": "surface", "name": "Длина резьбы 20мм",
             "dimensions": [{"dim_type": "linear", "nominal": 20.0}], "surfaces": []},
        ],
        "gdt": [],
    },
]


# ── Download helpers ──────────────────────────────────────────────────────────


def _download(url: str, dest: Path, timeout: int = 30) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            dest.write_bytes(r.read())
        return True
    except Exception as exc:
        print(f"  WARN: не удалось скачать {url}: {exc}", file=sys.stderr)
        return False


# ── Main ──────────────────────────────────────────────────────────────────────


def main(output_dir: Path, skip_download: bool = False) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ground_truth: list[dict] = []

    # 1. Real drawings
    if not skip_download:
        print("=== Скачивание реальных чертежей ===")
        for item in REAL_DRAWINGS:
            dest = output_dir / item["filename"]
            if dest.exists():
                print(f"  SKIP {item['filename']} (уже есть)")
            else:
                print(f"  → {item['filename']} ...")
                ok = _download(item["url"], dest)
                if ok:
                    print(f"    OK  ({dest.stat().st_size // 1024} KB)")
                    time.sleep(0.5)
            if dest.exists() and dest.stat().st_size > 1000:
                gt = {k: v for k, v in item.items() if k != "url"}
                gt.setdefault("source", "real")
                ground_truth.append(gt)

    # 2. Synthetic drawings
    print("\n=== Генерация синтетических чертежей ===")
    for item in SYNTHETIC_DRAWINGS:
        dest = output_dir / item["filename"]
        if dest.exists():
            print(f"  SKIP {item['filename']} (уже есть)")
        else:
            print(f"  → {item['filename']} ...")
            try:
                data = item["generator"]()
                if data:
                    dest.write_bytes(data)
                    print(f"    OK  ({dest.stat().st_size // 1024} KB)")
                else:
                    print(f"    SKIP (генератор вернул пустые данные — нет зависимости?)")
                    continue
            except Exception as exc:
                print(f"    ERROR: {exc}", file=sys.stderr)
                continue

        if dest.exists() and dest.stat().st_size > 100:
            gt = {
                "filename": item["filename"],
                "drawing_type": item["drawing_type"],
                "description": item["description"],
                "features": item.get("features", []),
                "source": "synthetic",
            }
            if item.get("gdt"):
                gt["gdt"] = item["gdt"]
            ground_truth.append(gt)

    # 3. Write ground truth
    gt_path = output_dir / "ground_truth.json"
    gt_path.write_text(json.dumps(ground_truth, ensure_ascii=False, indent=2))
    print(f"\n=== ground_truth.json записан: {len(ground_truth)} чертежей ===")

    # Summary
    print(f"\nДатасет: {output_dir}")
    for f in sorted(output_dir.iterdir()):
        if f.suffix in (".json",):
            continue
        kb = f.stat().st_size // 1024
        print(f"  {f.name:<40} {kb:>5} KB")
    print(f"\nИтого: {len(list(output_dir.glob('*')))-1} файлов + ground_truth.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download and generate engineering drawing test dataset")
    parser.add_argument("--output-dir", default="example-drawings", help="Output directory (default: example-drawings)")
    parser.add_argument("--skip-download", action="store_true", help="Skip real drawing downloads, only generate synthetic")
    args = parser.parse_args()
    main(Path(args.output_dir), skip_download=args.skip_download)
