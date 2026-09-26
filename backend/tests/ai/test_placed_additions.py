"""Элементы на сечениях, которых ридер не выписал, — по надписям листа (У6)."""

from __future__ import annotations

import math

from app.ai.cad_recognize.verifiers.reconcile import apply_placed_additions, placed_additions

OUTER = [
    {"diameter_mm": 28.0, "length_mm": 12.0},
    {"diameter_mm": 25.0, "length_mm": 70.0},
    {"diameter_mm": 40.0, "length_mm": 70.0},
]


def _spec(dimensions: list[str]) -> dict:
    return {"dimensions": dimensions, "main_view": {"outer": [dict(s) for s in OUTER]}}


HOLE = {
    "kind": "hole",
    "station_mm": 145.3,  # след секущей
    "step_index": 2,
    "step_diameter_mm": 40.0,
    "angle_deg": 59.8,
    "angles_deg": [59.8, 239.6],
    "diameter_mm": 3.1,
    "through": True,
    "tolerance_mm": 0.8,
}
FLAT = {
    "kind": "pocket",
    "station_mm": 62.9,
    "step_index": 1,
    "step_diameter_mm": 25.0,
    "angle_deg": 0.4,
    "depth_mm": 1.35,
    "across_mm": 23.65,
    "length_mm": 22.4,
    "tolerance_mm": 0.8,
}


def test_a_through_hole_is_added_when_every_number_is_on_the_sheet():
    spec = _spec(["12", "70", "262", "Ø25", "Ø40", "63.4", "Ø3", "60°"])
    notes: list[str] = []

    added = placed_additions(spec, {"placed_proposals": [HOLE]}, notes)

    assert not notes, notes
    hole = added[0]["feature"]
    assert hole["kind"] == "hole" and hole["diameter_mm"] == 3.0 and hole["through"] is True
    assert hole["origin_mm"][2] == 145.4  # 82 + 63,4 — число листа, не замер следа
    assert abs(math.degrees(math.atan2(hole["origin_mm"][1], hole["origin_mm"][0])) - 60.0) < 1e-3
    spec2 = apply_placed_additions(spec, added)
    assert len(spec2["main_view"]["placed_features"]) == 1


def test_an_angle_without_an_angle_label_is_refused():
    notes: list[str] = []
    added = placed_additions(
        _spec(["12", "70", "Ø3", "63.4", "60"]), {"placed_proposals": [HOLE]}, notes
    )

    assert added == [] and "угол" in notes[0]


def test_a_flat_takes_its_offset_and_length_from_one_pair_of_labels():
    spec = _spec(["12", "70", "23.7", "39.65", "22.5", "0.5×45°"])
    added = placed_additions(spec, {"placed_proposals": [FLAT]}, [])

    flat = added[0]["feature"]
    assert flat["width_mm"] == 22.5 and abs(flat["depth_mm"] - 1.3) < 1e-6
    assert flat["origin_mm"][2] == 12 + 39.65 + 22.5 / 2


def test_an_ambiguous_pair_is_refused():
    """Две пары «от уступа + длина» дают станцию почти одинаково — отказ."""
    notes: list[str] = []
    spec = _spec(["12", "70", "23.7", "39.65", "22.5", "39.8", "22.3"])
    flat = dict(FLAT, length_mm=None)

    assert placed_additions(spec, {"placed_proposals": [flat]}, notes) == []
    assert "однозначно" in notes[0]
