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
