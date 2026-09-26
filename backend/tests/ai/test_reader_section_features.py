"""Ридер: лыски и радиальные отверстия по вынесенным сечениям → placed_features (У6)."""

from __future__ import annotations

import asyncio
import math

import app.ai.cad_recognize.spec_fragments as fragments

OUTER = [
    {"diameter_mm": 25.0, "length_mm": 50.0},
    {"diameter_mm": 40.0, "length_mm": 60.0},
    {"diameter_mm": 25.0, "length_mm": 15.0},
]
CALLOUTS = {
    "dimensions": [
        {"value": "Б-Б"},
        {"value": "В-В"},
        {"value": "33.9"},
        {"value": "20"},
        {"value": "10"},
        {"value": "60°"},
        {"value": "Ø6 гл.8"},
    ]
}


def _read(answer: dict, callouts: dict = CALLOUTS) -> tuple[list[dict], list[str], list[str]]:
    asked: list[str] = []

    async def fake_ask(prompt, image, **kwargs):
        asked.append(prompt)
        return answer

    original = fragments._ask
    fragments._ask = fake_ask
    notes: list[str] = []
    try:
        placed = asyncio.run(
            fragments._read_section_features(
                None, OUTER, callouts, router=None, confidential=True, audit=None, notes=notes
            )
        )
    finally:
        fragments._ask = original
    return placed, notes, asked


def test_a_flat_and_a_blind_radial_hole_become_placed_features():
    placed, notes, _asked = _read(
        {
            "sections": [
                {
                    "label": "Б-Б",
                    "step_diameter_mm": 40,
                    "flats": [
                        {"angle_deg": 0, "across_mm": 33.9, "from_shoulder_mm": 10, "length_mm": 20}
                    ],
                },
                {
                    "label": "В-В",
                    "step_diameter_mm": 25,
                    "holes": [
                        {
                            "angle_deg": 60,
                            "diameter_mm": 6,
                            # 20 от уступа: помещается только в первую ступень Ø25.
                            "from_shoulder_mm": 20,
                            "through": False,
                            "depth_mm": 8,
                        }
                    ],
                },
            ]
        }
    )

    assert not notes, notes
    flat, hole = placed
    assert flat["kind"] == "pocket" and abs(flat["depth_mm"] - 6.1) < 1e-6  # 2·20 − 33,9
    assert flat["origin_mm"] == [20.0, 0.0, 70.0] and flat["width_mm"] == 20.0  # 50 + 10 + 20/2
    assert hole["origin_mm"][2] == 20.0
    assert hole["kind"] == "hole" and hole["through"] is False and hole["depth_mm"] == 8
    assert abs(math.degrees(math.atan2(hole["origin_mm"][1], hole["origin_mm"][0])) - 60) < 1e-3
    assert abs(math.hypot(hole["origin_mm"][0], hole["origin_mm"][1]) - 12.5) < 1e-3


def test_numbers_not_on_the_sheet_are_refused():
    placed, notes, _asked = _read(
        {
            "sections": [
                {
                    "label": "Б-Б",
                    "step_diameter_mm": 40,
                    "flats": [
                        {
                            "angle_deg": 45,
                            "across_mm": 33.9,
                            "from_shoulder_mm": 10,
                            "length_mm": 20,
                        }
                    ],
                    "holes": [{"angle_deg": 60, "diameter_mm": 7, "from_shoulder_mm": 20}],
                }
            ]
        }
    )

    assert placed == []  # угла 45° и Ø7 на листе нет
    assert len(notes) == 2


def test_no_section_labels_no_question():
    placed, _notes, asked = _read({"sections": []}, {"dimensions": [{"value": "Ø25"}]})

    assert placed == [] and asked == []


def test_a_section_label_with_a_non_breaking_hyphen_is_recognised():
    """Живой turned_multiaxis-0: модель пишет «Б‑Б» с U+2011."""
    assert fragments._SECTION_LABEL.search("разрез Б\u2011Б")
    assert fragments._SECTION_LABEL.search("Д—Д")
    assert not fragments._SECTION_LABEL.search("3-3")


def test_an_ambiguous_step_is_left_to_the_operator():
    """Две ступени Ø25: «10 от уступа» помещается в обе — станция неоднозначна."""
    placed, notes, _asked = _read(
        {
            "sections": [
                {
                    "label": "Б-Б",
                    "step_diameter_mm": 25,
                    "holes": [{"angle_deg": 60, "diameter_mm": 6, "from_shoulder_mm": 10}],
                }
            ]
        }
    )

    assert placed == [] and "однозначно" in notes[0]


def test_an_ambiguous_step_is_settled_by_the_order_of_section_letters():
    """Живой turned_multiaxis-0: Г-Г на Ø25 при двух ступенях Ø25 — между В и Д."""
    placed, notes, _asked = _read(
        {
            "sections": [
                {
                    "label": "Б-Б",
                    "step_diameter_mm": 40,
                    "holes": [{"angle_deg": 60, "diameter_mm": 6, "from_shoulder_mm": 20}],
                },
                {
                    "label": "В-В",
                    "step_diameter_mm": 25,
                    "holes": [{"angle_deg": 60, "diameter_mm": 6, "from_shoulder_mm": 10}],
                },
            ]
        }
    )

    assert not notes, notes
    assert [round(item["origin_mm"][2], 1) for item in placed] == [70.0, 120.0]


def test_a_standalone_angle_callout_triggers_the_question_but_a_chamfer_does_not():
    _placed, _notes, asked = _read({"sections": []}, {"dimensions": [{"value": "45°"}]})
    assert asked
    _placed, _notes, asked = _read({"sections": []}, {"dimensions": [{"value": "0.5×45°"}]})
    assert not asked
