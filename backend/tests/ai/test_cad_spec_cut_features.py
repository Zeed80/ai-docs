"""Reading the small features — and refusing the ones that cannot be there.

They are asked for separately and last, because mixing them into "describe the
whole contour" is what made the previous contract give up on them: the reader
was told to leave them out precisely because including them derailed the
profile.

Everything that comes back is checked against the contour already read. A
groove 900 mm along a 470 mm shaft is a misread, not a feature — and one bad
entry must not cost the rest.
"""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.spec_fragments import _read_cut_features

_OUTER = [
    {"diameter_mm": 80.0, "length_mm": 150.0},
    {"diameter_mm": 102.0, "length_mm": 200.0},
    {"diameter_mm": 60.0, "length_mm": 120.0},
]  # 470 mm long, biggest radius 51 mm


async def _read(monkeypatch, answer: dict) -> dict:
    async def fake_ask(*_a, **_kw):
        return answer

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    return await _read_cut_features(
        object(), _OUTER, router=object(), confidential=True
    )


@pytest.mark.asyncio
async def test_the_features_a_real_shaft_has_are_kept(monkeypatch):
    result = await _read(monkeypatch, {
        "chamfers": [{"size_mm": 1.0, "angle_deg": 45.0, "location": "left_end"}],
        "grooves": [{"axial_position_mm": 250.0, "width_mm": 3.0, "depth_mm": 1.5}],
        "keyways": [{"axial_start_mm": 40.0, "length_mm": 85.0,
                     "width_mm": 12.0, "depth_mm": 5.0}],
        "cross_holes": [{"diameter_mm": 9.0, "axial_position_mm": 200.0,
                         "count": 2, "through": True}],
    })
    assert len(result["chamfers"]) == 1
    assert len(result["grooves"]) == 1
    assert len(result["keyways"]) == 1
    assert len(result["cross_holes"]) == 1


@pytest.mark.asyncio
async def test_a_feature_off_the_end_of_the_part_is_dropped(monkeypatch):
    result = await _read(monkeypatch, {
        "grooves": [
            {"axial_position_mm": 900.0, "width_mm": 3.0, "depth_mm": 1.5},
            {"axial_position_mm": 250.0, "width_mm": 3.0, "depth_mm": 1.5},
        ],
    })
    # The impossible one goes; the real one stays.
    assert [g["axial_position_mm"] for g in result["grooves"]] == [250.0]


@pytest.mark.asyncio
async def test_a_cut_deeper_than_the_shaft_is_dropped(monkeypatch):
    result = await _read(monkeypatch, {
        "keyways": [{"axial_start_mm": 40.0, "length_mm": 85.0,
                     "width_mm": 12.0, "depth_mm": 60.0}],
        "grooves": [{"axial_position_mm": 250.0, "width_mm": 3.0, "depth_mm": 80.0}],
    })
    assert "keyways" not in result and "grooves" not in result


@pytest.mark.asyncio
async def test_a_keyway_shorter_than_it_is_wide_is_dropped(monkeypatch):
    result = await _read(monkeypatch, {
        "keyways": [{"axial_start_mm": 40.0, "length_mm": 5.0,
                     "width_mm": 12.0, "depth_mm": 5.0}],
    })
    assert "keyways" not in result


@pytest.mark.asyncio
async def test_a_chamfer_with_no_place_is_dropped(monkeypatch):
    """"Where" is what a chamfer needs; a size alone cannot be placed."""
    result = await _read(monkeypatch, {
        "chamfers": [
            {"size_mm": 1.0, "location": "somewhere"},
            {"size_mm": 1.0, "location": "right_end"},
        ],
    })
    assert [c["location"] for c in result["chamfers"]] == ["right_end"]


@pytest.mark.asyncio
async def test_a_sheet_with_no_such_features_yields_nothing(monkeypatch):
    assert await _read(monkeypatch, {
        "chamfers": [], "grooves": [], "keyways": [], "cross_holes": []
    }) == {}


@pytest.mark.asyncio
async def test_a_failed_question_costs_only_itself(monkeypatch):
    assert await _read(monkeypatch, {}) == {}
