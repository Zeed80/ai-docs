"""Прорезь пластины по плану (X1): центр, длина, ширина."""

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers import plate_slot
from app.ai.cad_recognize.verifiers.view_frame import ViewFrame

# План 100 × 60 мм, 10 px на мм: левый нижний угол — (100, 700).
_FRAME = ViewFrame(bbox_px=(100, 100, 1100, 700), mm_per_px=0.1, origin_px=(100.0, 700.0))


def _capsule(draw, cx, cy, length, width, vertical=False):
    """Капсула в px: центр, габаритная длина, ширина."""
    r = width / 2.0
    half = length / 2.0 - r
    if vertical:
        draw.arc([cx - r, cy - half - r, cx + r, cy - half + r], 180, 360, fill=0, width=4)
        draw.arc([cx - r, cy + half - r, cx + r, cy + half + r], 0, 180, fill=0, width=4)
        draw.line([(cx - r, cy - half), (cx - r, cy + half)], fill=0, width=4)
        draw.line([(cx + r, cy - half), (cx + r, cy + half)], fill=0, width=4)
    else:
        draw.arc([cx - half - r, cy - r, cx - half + r, cy + r], 90, 270, fill=0, width=4)
        draw.arc([cx + half - r, cy - r, cx + half + r, cy + r], 270, 450, fill=0, width=4)
        draw.line([(cx - half, cy - r), (cx + half, cy - r)], fill=0, width=4)
        draw.line([(cx - half, cy + r), (cx + half, cy + r)], fill=0, width=4)


def _sheet() -> np.ndarray:
    image = Image.new("L", (1200, 800), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle(_FRAME.bbox_px, outline=0, width=6)
    # Прорезь 30 × 8 мм с центром (−20; 10) от центра плана и вертикальная
    # 25 × 6 мм с центром (25; −5).
    _capsule(draw, 100 + 300, 700 - 400, 300, 80)
    _capsule(draw, 100 + 750, 700 - 250, 250, 60, vertical=True)
    return np.asarray(image)


def _profile(**slot_overrides):
    slots = [
        {"center_x_mm": -20.0, "center_y_mm": 10.0, "length_mm": 30.0, "width_mm": 8.0},
        {
            "center_x_mm": 25.0,
            "center_y_mm": -5.0,
            "length_mm": 25.0,
            "width_mm": 6.0,
            "rotation_deg": 90.0,
        },
    ]
    slots[0].update(slot_overrides)
    return {"shape": "rectangle", "width_mm": 100.0, "height_mm": 60.0, "slots": slots}


def test_slots_are_measured_horizontal_and_vertical(monkeypatch):
    monkeypatch.setattr(
        "app.ai.cad_recognize.verifiers.plate_frame.locate_plate_frame", lambda *_a, **_k: _FRAME
    )

    items = plate_slot.verify_plate_slots(_sheet(), _profile())

    assert [item["status"] for item in items] == ["confirmed", "confirmed"]
    assert abs(items[1]["measured"]["length_mm"] - 25.0) <= 0.4
    assert abs(items[1]["measured"]["center_y_mm"] + 5.0) <= 0.3


def test_a_slot_read_a_little_off_is_refuted_and_far_off_is_another_object(monkeypatch):
    monkeypatch.setattr(
        "app.ai.cad_recognize.verifiers.plate_frame.locate_plate_frame", lambda *_a, **_k: _FRAME
    )

    shifted = plate_slot.verify_plate_slots(_sheet(), _profile(center_y_mm=12.0))[0]
    wider = plate_slot.verify_plate_slots(_sheet(), _profile(width_mm=10.0))[0]

    assert shifted["status"] == "refuted" and abs(shifted["measured"]["center_y_mm"] - 10.0) <= 0.3
    assert wider["status"] == "refuted" and abs(wider["measured"]["width_mm"] - 8.0) <= 0.3
