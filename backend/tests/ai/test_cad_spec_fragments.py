"""Fragment reading: narrow questions, isolated failures."""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.spec_fragments import (
    _has_geometry,
    _stamp_crop,
    _type_label,
    read_spec_best_effort,
)


def test_the_stamp_crop_is_the_bottom_right_corner():
    """ГОСТ 2.104 puts it there; a corner crop is an easy question."""
    from PIL import Image

    sheet = Image.new("RGB", (2000, 1400))
    crop = _stamp_crop(sheet)
    assert crop.size[0] < sheet.size[0] / 2
    assert crop.size[1] < sheet.size[1] / 2


def test_kind_maps_to_the_body_type_the_contract_uses():
    assert "вращ" in _type_label("rotation")
    assert _type_label("flange") == "фланец"
    assert _type_label("") == ""


def test_geometry_presence_covers_both_supported_classes():
    assert _has_geometry({"main_view": {"outer": [{"diameter_mm": 30}]}})
    assert _has_geometry({"main_view": {"profile": {"shape": "circle"}}})
    assert not _has_geometry({"main_view": {"profile": {}}})
    assert not _has_geometry({"main_view": {}})


@pytest.mark.asyncio
async def test_fragments_win_when_they_produced_geometry(monkeypatch):
    fragment_spec = {
        "main_view": {"profile": {"shape": "circle", "diameter_mm": 560}},
        "title_block": {"material": "Чугун СЧ20"},
        "fragments": {"geometry": True},
    }
    called: list[str] = []

    async def fake_fragments(*_a, **_k):
        called.append("fragments")
        return fragment_spec

    async def fake_whole(*_a, **_k):
        called.append("whole")
        return {"main_view": {"outer": [{"diameter_mm": 1, "length_mm": 1}]}}

    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_fragments.read_spec_by_fragments", fake_fragments
    )
    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_vectorize.read_drawing_spec_consensus", fake_whole
    )
    result = await read_spec_best_effort(b"x")
    assert result is fragment_spec
    # The expensive whole-sheet read must not run when it is not needed.
    assert called == ["fragments"]


@pytest.mark.asyncio
async def test_the_fallback_runs_only_for_missing_geometry(monkeypatch):
    async def fake_fragments(*_a, **_k):
        return {
            "main_view": {},
            "title_block": {"material": "Сталь 45", "scale": "1:2"},
            "dimensions": [{"value": "Ø80js6"}],
            "fragments": {"geometry": False},
        }

    async def fake_whole(*_a, **_k):
        return {
            "main_view": {"outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": 60},
            ]},
            "title_block": {},
        }

    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_fragments.read_spec_by_fragments", fake_fragments
    )
    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_vectorize.read_drawing_spec_consensus", fake_whole
    )
    result = await read_spec_best_effort(b"x")
    assert len(result["main_view"]["outer"]) == 2
    # The stamp read off a crop beats the one the whole-sheet pass missed.
    assert result["title_block"]["material"] == "Сталь 45"
    assert [d["value"] for d in result["dimensions"]] == ["Ø80js6"]


def test_standard_reference_numbers_are_not_dimension_candidates():
    """A standard's number is a citation, not a size.

    The reader is handed the sheet's numbers and told to pick its diameters
    and axial positions from that list only. On the spindle sheet the three
    largest entries were 19860, 2013 and 1050 — from "AT6 по ГОСТ 19860-73"
    and "Сталь 55 ГОСТ 1050-2013" — so the largest "dimension" it was offered
    was a standard from 1973.
    """
    from app.ai.cad_recognize.spec_fragments import _callout_numbers

    callouts = {
        "dimensions": [{"value": "Ø102h6"}, {"value": "470"}],
        "annotations": [
            {"text": "Сталь 55 ГОСТ 1050-2013"},
            {"text": "Точность конуса AT6 по ГОСТ 19860-73"},
        ],
    }

    numbers = _callout_numbers(callouts)

    assert 470.0 in numbers and 102.0 in numbers
    for citation in (19860.0, 2013.0, 1050.0, 73.0):
        assert citation not in numbers, f"{citation} is a standard, not a size"


def test_evenly_spaced_chain_is_refused_as_fabricated():
    """A chain whose every step is equal was invented, not read.

    Asked for the axial positions of a ten-step spindle, the reader answered
    0, 45, 90 ... 405: perfectly even, and not one of those numbers appears
    among the sheet's callouts. The count check passes such an answer, so the
    pathology needs its own guard — otherwise a part gets built from it.
    """
    import asyncio
    from unittest.mock import AsyncMock, patch

    from app.ai.cad_recognize import spec_fragments as fragments

    callouts = {"dimensions": [{"value": f"{v}"} for v in (470, 150, 78, 102, 80, 72)]}
    fabricated = {
        "diameters_mm": [102, 80, 80, 72, 72, 70, 68, 66, 64, 62],
        "chain_mm": [45 * i for i in range(1, 11)],
        "overall_mm": 470,
    }

    with patch.object(fragments, "_ask", AsyncMock(return_value=fabricated)):
        sections, problem = asyncio.run(
            fragments._sections_from_chain(None, callouts, router=None, confidential=True)
        )

    assert sections == []
    assert problem and "ровным шагом" in problem
