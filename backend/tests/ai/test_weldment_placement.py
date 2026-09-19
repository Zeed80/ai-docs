"""Размещение деталей сварного узла — по плану на листе (X3)."""

import copy

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize.verifiers.reconcile import (
    apply_weldment_placement,
    weldment_placement_decision,
)
from app.ai.cad_recognize.verifiers.weldment_placement import verify_weldment_placement

PX = 4.0  # px на мм
X0, Y0 = 200, 150  # левый верхний угол плана основания на листе

SPEC = {
    "parts": [
        {
            "profile": {
                "shape": "rectangle",
                "width_mm": 100.0,
                "height_mm": 120.0,
                "thickness_mm": 10.0,
            }
        },
        {
            "profile": {
                "shape": "rectangle",
                "width_mm": 100.0,
                "height_mm": 50.0,
                "thickness_mm": 5.0,
            },
            "placement": {
                "position_mm": [0.0, 120.0, 10.0],
                "axis": [1.0, 0.0, 0.0],
                "angle_deg": 90.0,
            },
        },
    ],
    "welds": [{"bodies": [0, 1], "leg_mm": 4.0, "both_sides": False}],
    "dimensions": [{"value": v} for v in ("100", "120", "10", "50", "5", "115")],
}


def _plan() -> np.ndarray:
    """План: основание 100 × 120, ребро у дальнего края (y 115…120), валик до 111."""
    image = Image.new("L", (1000, 900), 255)
    draw = ImageDraw.Draw(image)

    def y_px(y_mm: float) -> float:  # ось y плана вверх, листа — вниз
        return Y0 + (120.0 - y_mm) * PX

    draw.rectangle((X0, Y0, X0 + 100 * PX, Y0 + 120 * PX), outline=0, width=3)
    for y in (115.0, 111.0):
        draw.line((X0, y_px(y), X0 + 100 * PX, y_px(y)), fill=0, width=3)
    return np.asarray(image)


def _read_at(y: float) -> dict:
    spec = copy.deepcopy(SPEC)
    spec["parts"][1]["placement"]["position_mm"][1] = y
    return spec


def test_the_rib_where_the_plan_shows_it_is_confirmed():
    items = verify_weldment_placement(_plan(), _read_at(120.0))
    assert [i["status"] for i in items] == ["confirmed"]


def test_a_rib_read_at_the_near_edge_is_moved_where_the_plan_and_the_label_put_it():
    """Живой узел: ребро прочитано у ближнего края (y = 10 — толщина основания),
    на листе — у дальнего, размер 115; узел собрался молча не тем."""
    spec = _read_at(10.0)
    item = verify_weldment_placement(_plan(), spec)[0]
    assert item["status"] == "refuted"
    decision = weldment_placement_decision(spec, item)
    assert decision["action"] == "adopt"
    fixed = apply_weldment_placement(spec, decision)
    assert abs(fixed["parts"][1]["placement"]["position_mm"][1] - 120.0) <= 0.5


def test_without_a_label_for_the_place_it_goes_to_a_person():
    spec = _read_at(10.0)
    spec["dimensions"] = [d for d in spec["dimensions"] if d["value"] != "115"]
    item = verify_weldment_placement(_plan(), spec)[0]
    assert weldment_placement_decision(spec, item)["action"] == "ask_human"
