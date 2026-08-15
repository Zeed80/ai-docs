"""Fragment reading: narrow questions, isolated failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.cad_recognize.spec_fragments import (
    _clean_callout_observations,
    _detect_pmi_frame_regions,
    _flag_unconfirmed_outer_bore_diameters,
    _observation_only_spec,
    _has_geometry,
    _is_sheet_metadata_line,
    _mark_observation_only_if_no_geometry,
    _pmi_contact_sheet,
    _structured_pmi_annotations,
    _enrich_post_consensus_source_geometry,
    _stamp_crop,
    _type_label,
    read_spec_best_effort,
)


def test_post_consensus_restores_measured_m8_entry_plane_from_source():
    image = (
        Path(__file__).resolve().parents[3]
        / "test_vector_files" / "detal_126.png"
    ).read_bytes()
    spec = {
        "main_view": {"axial_holes": [{
            "count": 2,
            "bolt_circle_diameter_mm": 80,
            "from_face": "zmin",
            "entry_offset_mm": None,
            "entry_recess_diameter_mm": None,
            "thread": {"nominal_diameter_mm": 8},
            "evidence": [{"bbox": [2126, 465, 2146, 722]}],
        }]},
        "dimensions": [{"value": "470"}, {"value": "Ø80"}],
        "annotations": [],
        "unresolved": [],
    }

    result = _enrich_post_consensus_source_geometry(spec, image)

    assert result["main_view"]["axial_holes"][0]["entry_offset_mm"] == 5.6
    assert any("Ø входной выборки" in item for item in result["unresolved"])


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


def test_sheet_metadata_is_not_a_dimension_and_empty_pmi_does_not_kill_reading():
    callouts, dropped = _clean_callout_observations({
        "dimensions": [
            {"value": "Ø25 ±0.15"},
            {"value": "NIST PMI Test Models - 2012"},
            {"value": "Test Model 1"},
            {"value": "Масштаб 1:2"},
        ],
        "annotations": [
            {"kind": "tolerance", "text": "", "datum_refs": ["A"]},
            {"kind": "tolerance", "text": "0.75 | A | B | C"},
        ],
    })
    assert [item["value"] for item in callouts["dimensions"]] == ["Ø25 ±0.15"]
    assert [item["text"] for item in callouts["annotations"]] == ["0.75 | A | B | C"]
    assert dropped == 1
    assert _is_sheet_metadata_line("Лист 1")
    assert not _is_sheet_metadata_line("4X M8x1.25")


def test_geometry_free_valid_spec_is_explicitly_observation_only():
    spec = {"main_view": {}, "dimensions": [{"value": "Ø25"}]}
    assert _mark_observation_only_if_no_geometry(spec)["observation_only"] is True


def test_structured_pmi_preserves_characteristic_value_and_datum_order():
    annotations, unresolved = _structured_pmi_annotations({
        "frames": [
            {
                "characteristic": "profile_surface",
                "tolerance_text": "0.75 A B C",
                "datum_refs": ["A", "B", "C"],
            },
            {
                "characteristic": "perpendicularity",
                "tolerance_text": "Ø0.02Ⓜ | A",
                "datum_refs": ["A"],
            },
            {"characteristic": "unknown", "tolerance_text": "0.5", "datum_refs": []},
        ]
    })
    assert annotations == [
        {
            "kind": "tolerance",
            "text": "⌓ | 0.75 | A | B | C",
            "value": "0.75",
            "symbol": "profile_surface",
            "datum_refs": ["A", "B", "C"],
        },
        {
            "kind": "tolerance",
            "text": "⏊ | Ø0.02Ⓜ | A",
            "value": "Ø0.02Ⓜ",
            "symbol": "perpendicularity",
            "datum_refs": ["A"],
        },
    ]
    assert unresolved == 1


def test_pmi_contact_sheet_maps_normalized_box_to_original_evidence():
    from PIL import Image

    image = Image.new("RGB", (1000, 500), "white")
    sheet, evidence = _pmi_contact_sheet(
        image,
        {"frames": [{"bbox": [100, 200, 300, 400]}]},
        (50, 20, 950, 420),
    )

    assert sheet is not None
    assert evidence[1]["bbox"] == [140.0, 100.0, 320.0, 180.0]

    annotations, unresolved = _structured_pmi_annotations(
        {"frames": [
            {
                "frame_id": 1,
                "characteristic": "flatness",
                "tolerance_text": "0.2",
                "datum_refs": [],
            },
            {
                "frame_id": 99,
                "characteristic": "position",
                "tolerance_text": "Ø0.5",
                "datum_refs": ["A"],
            },
        ]},
        evidence,
    )

    assert annotations[0]["text"] == "▱ | 0.2"
    assert annotations[0]["evidence"] == [evidence[1]]
    assert unresolved == 1


def test_pmi_contact_sheet_rejects_degenerate_locator_boxes():
    from PIL import Image

    sheet, evidence = _pmi_contact_sheet(
        Image.new("RGB", (1000, 500), "white"),
        {"frames": [
            {"bbox": [100, 100, 100, 200]},
            {"bbox": [200, 200, 204, 202]},
            {"bbox": ["bad", 0, 10, 10]},
        ]},
        (0, 0, 1000, 500),
    )

    assert sheet is None
    assert evidence == {}


def test_deterministic_pmi_locator_groups_adjacent_rotated_cells_only():
    import cv2
    import numpy as np
    from PIL import Image

    canvas = np.full((500, 1000), 255, dtype=np.uint8)
    for center in ((300, 220), (352, 190)):
        points = cv2.boxPoints((center, (62, 28), -30)).astype(np.int32)
        cv2.polylines(canvas, [points], True, 0, 2)
    isolated = cv2.boxPoints(((700, 300), (62, 28), -30)).astype(np.int32)
    cv2.polylines(canvas, [isolated], True, 0, 2)

    result = _detect_pmi_frame_regions(Image.fromarray(canvas).convert("RGB"))

    assert len(result["frames"]) == 1
    assert result["frames"][0]["source"] == "deterministic_cv"


def test_invalid_geometry_preserves_pmi_as_unresolved_observations():
    assembled = {
        "part": "test part",
        "main_view": {"type": "prismatic", "profile": {"shape": "rectangle"}},
        "dimensions": [
            {"value": "⌀25 ±0.15", "applies_to": None, "evidence": [{"image_index": 1, "bbox": [1, 2, 3, 4]}]},
            {"value": ""},
        ],
        "annotations": [
            {"kind": "datum", "text": "A", "evidence": []},
            {"kind": "tolerance", "text": "", "value": "⌓ | 0.5 | A"},
            {"kind": "other", "text": ""},
        ],
        "title_block": {"name": "test part"},
        "unresolved": [],
    }

    result = _observation_only_spec(
        assembled,
        fragments={"callouts": True},
        fragment_answers=[{"task": "callouts"}],
        invalid_fields=["main_view.profile"],
    )

    assert result["observation_only"] is True
    assert result["main_view"]["type"] == "unknown"
    assert result["main_view"]["profile"] is None
    assert [item["value"] for item in result["dimensions"]] == ["⌀25 ±0.15"]
    assert [item["text"] for item in result["annotations"]] == ["A", "⌓ | 0.5 | A"]
    assert result["geometry_validation_errors"] == ["main_view.profile"]
    assert "geometry_schema_invalid:main_view.profile" in result["unresolved"]

    from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec

    round_trip = EngineeringDrawingSpec.model_validate(result).model_dump(mode="json")
    assert round_trip["observation_only"] is True
    assert round_trip["geometry_validation_errors"] == ["main_view.profile"]


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
    result = await read_spec_best_effort(b"x", passes=3)
    assert result["main_view"]["profile"]["diameter_mm"] == 560
    assert result["title_block"]["material"] == "Чугун СЧ20"
    # Two independent matching reads are enough. A third full pass would only
    # consume the global budget and used to erase both valid reads on timeout.
    assert called == ["fragments"] * 2
    assert result["consensus"]["usable"] == 2
    # Two identical reads are not a disagreement about anything.
    assert result["unresolved"] == []


@pytest.mark.asyncio
async def test_fragment_passes_that_disagree_do_not_ship_a_lucky_read(monkeypatch):
    """The value that changes between passes is exactly the one to withhold."""
    reads = [
        {"main_view": {"outer": [
            {"diameter_mm": 30, "length_mm": 40},
            {"diameter_mm": 50, "length_mm": 60},
        ]}},
        {"main_view": {"outer": [
            {"diameter_mm": 30, "length_mm": 40},
            {"diameter_mm": 50, "length_mm": 95},
        ]}},
        {"main_view": {"outer": [{"diameter_mm": 30, "length_mm": 40}]}},
    ]
    order = iter(reads)

    async def fake_fragments(*_a, **_k):
        return next(order)

    async def fake_whole(*_a, **_k):
        return {}

    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_fragments.read_spec_by_fragments", fake_fragments
    )
    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_vectorize.read_drawing_spec_consensus", fake_whole
    )
    result = await read_spec_best_effort(b"x", passes=3)
    assert not (result.get("main_view") or {}).get("outer")
    assert any("профил" in item for item in result["unresolved"])


@pytest.mark.asyncio
async def test_the_fallback_runs_for_missing_geometry(monkeypatch):
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


@pytest.mark.asyncio
async def test_fallback_grounds_whole_sheet_in_fragments_own_confirmed_diameters(
    monkeypatch,
):
    """Live-found (shaft_detail.png): the whole-sheet reader's schema omits
    evidence entirely (token-budget reasons), so on its own it has no way to
    check an outer/bore diameter against the sheet — it invented a Ø70 that
    appears nowhere on the drawing while the fragment pass's OWN dimensions
    list, read moments earlier by the SAME pipeline, correctly had
    Ø30/Ø50/Ø30. read_spec_best_effort must forward what fragments already
    confirmed into the whole-sheet call as known_diameters_mm."""
    captured: dict = {}

    async def fake_fragments(*_a, **_k):
        return {
            "main_view": {},
            "dimensions": [
                {"value": "Ø50h6"}, {"value": "Ø30h6"}, {"value": "Ø30k6"},
                {"value": "220"},  # not Ø-marked -- must not be treated as a diameter
            ],
            "fragments": {"geometry": False},
        }

    async def fake_whole(*_a, **kwargs):
        captured["known_diameters_mm"] = kwargs.get("known_diameters_mm")
        return {"main_view": {"outer": [{"diameter_mm": 50, "length_mm": 220}]}}

    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_fragments.read_spec_by_fragments", fake_fragments
    )
    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_vectorize.read_drawing_spec_consensus", fake_whole
    )
    await read_spec_best_effort(b"x")

    assert sorted(captured["known_diameters_mm"]) == [30.0, 50.0]


@pytest.mark.asyncio
async def test_unresolved_fragment_geometry_triggers_whole_sheet_fallback(monkeypatch):
    fragment_spec = {
        "main_view": {"outer": [{"diameter_mm": 80, "length_mm": 597.2}]},
        "title_block": {"material": "Сталь 55"},
        "unresolved": ["сумма ступеней 597.2 мм больше габарита 470 мм"],
    }
    whole_spec = {
        "main_view": {"outer": [{"diameter_mm": 80, "length_mm": 470}]},
        "title_block": {},
        "unresolved": [],
    }
    called = []

    async def fake_fragments(*_a, **_k):
        return fragment_spec

    async def fake_whole(*_a, **_k):
        called.append("whole")
        return whole_spec

    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_fragments.read_spec_by_fragments", fake_fragments
    )
    monkeypatch.setattr(
        "app.ai.cad_recognize.spec_vectorize.read_drawing_spec_consensus", fake_whole
    )

    result = await read_spec_best_effort(b"x", passes=3)

    assert called == ["whole"]
    assert result["main_view"]["outer"][0]["length_mm"] == 470
    assert result["title_block"]["material"] == "Сталь 55"


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


@pytest.mark.asyncio
async def test_dimension_chain_question_uses_the_parent_reader_audit(monkeypatch):
    from app.ai.cad_recognize import spec_fragments as fragments

    audit: list[dict] = []

    async def fake_ask(*_a, **kwargs):
        kwargs["audit"].append({
            "question": "dimension chain",
            "model": "test-reader",
            "raw_response": '{"diameters_mm":[30,40],"chain_mm":[20,50]}',
        })
        return {
            "diameters_mm": [30, 40],
            "chain_mm": [20, 50],
            "overall_mm": 50,
        }

    monkeypatch.setattr(fragments, "_ask", fake_ask)
    sections, problem = await fragments._sections_from_chain(
        None,
        {"dimensions": [{"value": "Ø30"}, {"value": "Ø40"}, {"value": "20"}, {"value": "50"}]},
        router=object(),
        confidential=True,
        audit=audit,
    )

    assert problem is None
    assert sections == [
        {"diameter_mm": 30.0, "length_mm": 20.0},
        {"diameter_mm": 40.0, "length_mm": 30.0},
    ]
    assert audit[0]["raw_response"].startswith('{"diameters_mm"')


@pytest.mark.asyncio
async def test_dimension_chain_receives_localized_datum_evidence(monkeypatch):
    from app.ai.cad_recognize import (
        axial_dimensions,
        diameter_dimensions,
        spec_fragments as fragments,
    )

    captured = {}

    def fake_localize(_image, _known):
        return {
            "status": "ok",
            "overall_mm": 50,
            "mm_per_px": 0.5,
            "blockers": [],
            "observations": [
                {
                    "id": "axial-dim-1",
                    "value_mm": 20,
                    "raw_text": "20",
                    "ocr_corrected": False,
                    "relation": "from_left_datum",
                    "station_from_left_mm": 20,
                    "label_bbox": [10, 5, 20, 10],
                    "dimension_line": [0, 15, 40, 15],
                    "confidence": 0.95,
                },
                {
                    "id": "axial-dim-2",
                    "value_mm": 50,
                    "raw_text": "50",
                    "ocr_corrected": False,
                    "relation": "overall",
                    "station_from_left_mm": 50,
                    "label_bbox": [20, 30, 30, 35],
                    "dimension_line": [0, 40, 100, 40],
                    "confidence": 0.95,
                },
            ],
        }

    async def fake_ask(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return {
            "diameters_mm": [30, 40],
            "chain_mm": [20, 50],
            "overall_mm": 50,
        }

    monkeypatch.setattr(axial_dimensions, "localize_axial_dimensions", fake_localize)
    monkeypatch.setattr(
        diameter_dimensions,
        "localize_diameter_dimensions",
        lambda *_args: {
            "status": "ok",
            "observations": [
                {"value_mm": 30, "role": "outer", "confidence": 0.95},
                {"value_mm": 40, "role": "outer", "confidence": 0.95},
            ],
            "outer_transition_stations": [],
            "blockers": [],
        },
    )
    monkeypatch.setattr(fragments, "_ask", fake_ask)

    sections, problem = await fragments._sections_from_chain(
        object(),
        {"dimensions": [{"value": "Ø30"}, {"value": "Ø40"}, {"value": "20"}, {"value": "50"}]},
        router=object(),
        confidential=True,
        source_image=object(),
    )

    assert problem is None
    assert sections
    assert '"relation":"from_left_datum"' in captured["prompt"]
    assert '"station_from_left_mm":20' in captured["prompt"]
    assert '"role":"outer"' in captured["prompt"]


@pytest.mark.asyncio
async def test_dimension_chain_rejects_diameter_assigned_to_wrong_contour(monkeypatch):
    from app.ai.cad_recognize import (
        axial_dimensions,
        diameter_dimensions,
        spec_fragments as fragments,
    )

    monkeypatch.setattr(
        axial_dimensions,
        "localize_axial_dimensions",
        lambda *_args: {
            "status": "ok",
            "observations": [
                {"station_from_left_mm": 20, "confidence": 0.95},
                {"station_from_left_mm": 50, "confidence": 0.95},
            ],
        },
    )
    monkeypatch.setattr(
        diameter_dimensions,
        "localize_diameter_dimensions",
        lambda *_args: {
            "status": "ok",
            "observations": [
                {"value_mm": 30, "role": "outer", "confidence": 0.95},
                {"value_mm": 40, "role": "bore", "confidence": 0.95},
            ],
        },
    )

    async def fake_ask(*_args, **_kwargs):
        return {
            "diameters_mm": [30, 40],
            "chain_mm": [20, 50],
            "overall_mm": 50,
        }

    monkeypatch.setattr(fragments, "_ask", fake_ask)
    sections, problem = await fragments._sections_from_chain(
        object(),
        {
            "dimensions": [
                {"value": "Ø30"}, {"value": "Ø40"},
                {"value": "20"}, {"value": "50"},
            ]
        },
        router=object(),
        confidential=True,
        source_image=object(),
    )

    assert sections == []
    assert problem and "наружные диаметры" in problem
    assert "Ø40" in problem


@pytest.mark.asyncio
async def test_dimension_chain_rejects_station_without_localized_line(monkeypatch):
    from app.ai.cad_recognize import axial_dimensions, spec_fragments as fragments

    monkeypatch.setattr(
        axial_dimensions,
        "localize_axial_dimensions",
        lambda *_args: {
            "status": "ok",
            "overall_mm": 50,
            "blockers": [],
            "observations": [
                {"station_from_left_mm": 20, "confidence": 0.95},
                {"station_from_left_mm": 50, "confidence": 0.95},
            ],
        },
    )

    async def fake_ask(*_args, **_kwargs):
        return {
            "diameters_mm": [30, 35, 40],
            "chain_mm": [20, 30, 50],
            "overall_mm": 50,
        }

    monkeypatch.setattr(fragments, "_ask", fake_ask)

    sections, problem = await fragments._sections_from_chain(
        object(),
        {
            "dimensions": [
                {"value": "Ø30"}, {"value": "Ø35"}, {"value": "Ø40"},
                {"value": "20"}, {"value": "30"}, {"value": "50"},
            ]
        },
        router=object(),
        confidential=True,
        source_image=object(),
    )

    assert sections == []
    assert problem and "не подтверждены локализованными" in problem
    assert "30" in problem


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


def test_whole_fallback_cannot_overwrite_verified_fragment_outer_or_restore_bore():
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragments = {
        "main_view": {
            "outer": [
                {"diameter_mm": 102, "length_mm": 14,
                 "note": "контур и осевая станция подтверждены OCR+CV",
                 "evidence": [{"image_index": 0, "bbox": [1, 2, 3, 4]}]},
                {"diameter_mm": 80, "length_mm": 357,
                 "note": "контур и осевая станция подтверждены OCR+CV",
                 "evidence": [{"image_index": 0, "bbox": [2, 2, 4, 4]}]},
                {"diameter_mm": 72, "length_mm": 99,
                 "note": "наружный резьбовой участок подтверждён размерной цепью",
                 "evidence": [{"image_index": 0, "bbox": [3, 2, 5, 4]}]},
            ],
        },
        "unresolved": [
            "расточка: диаметры расточки не подтверждены локализованным внутренним контуром: Ø12"
        ],
        "annotations": [{"kind": "hardness", "text": "HRC 58...62"}],
    }
    whole = {
        "main_view": {
            "outer": [{"diameter_mm": 65, "length_mm": None}],
            "bore": [{"diameter_mm": 12, "length_mm": 35}],
        },
        "unresolved": ["whole-sheet неполон"],
        "annotations": [{"kind": "material", "text": "Сталь 55"}],
    }

    merged = _merge_fragment_truth(whole, fragments)

    assert merged["main_view"]["outer"] == fragments["main_view"]["outer"]
    assert "bore" not in merged["main_view"]
    assert merged["unresolved"] == [
        "whole-sheet неполон",
        "расточка: диаметры расточки не подтверждены локализованным внутренним контуром: Ø12",
    ]
    assert whole["main_view"]["outer"][0]["diameter_mm"] == 65
    assert merged["annotations"] == [
        {"kind": "material", "text": "Сталь 55"},
        {"kind": "hardness", "text": "HRC 58...62"},
    ]


def test_whole_fallback_cannot_overwrite_verified_fragment_bore():
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    verified_bore = [
        {
            "diameter_mm": 56.55,
            "length_mm": 78,
            "note": "конус подтверждён контуром и отношением 7:24",
            "taper": {"kind": "ratio", "ratio": "7:24"},
            "evidence": [{"image_index": 0, "bbox": [1, 2, 3, 4]}],
        },
        {
            "diameter_mm": 51,
            "length_mm": 72,
            "note": "внутренний контур измерен и привязан к осевой станции",
            "evidence": [{"image_index": 0, "bbox": [2, 2, 4, 4]}],
        },
    ]
    fragments = {"main_view": {"bore": verified_bore}, "unresolved": []}
    whole = {
        "main_view": {"bore": [{"diameter_mm": 12, "length_mm": 35}]},
        "unresolved": [
            "body:0:bore:0:length-missing",
            "другой блокер",
        ],
    }

    merged = _merge_fragment_truth(whole, fragments)

    assert merged["main_view"]["bore"] == verified_bore
    assert merged["unresolved"] == ["другой блокер"]


def test_whole_fallback_cannot_restore_unverified_small_features():
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    verified_keyway = {
        "axial_start_mm": 277,
        "length_mm": 85,
        "width_mm": 12,
        "depth_mm": 5,
        "evidence": [{"image_index": 0, "bbox": [1, 2, 3, 4]}],
    }
    fragments = {
        "main_view": {"keyways": [verified_keyway]},
        "unresolved": [
            "малые элементы: keyway-2: глубина не подтверждена",
            "малые элементы: поперечное отверстие Ø14 указано, но не локализовано",
        ],
    }
    whole = {
        "main_view": {
            "keyways": [{"axial_start_mm": 10, "length_mm": 20}],
            "cross_holes": [{"diameter_mm": 14, "axial_position_mm": 60}],
            "chamfers": [{"size_mm": 2, "location": "right_end"}],
        },
        "unresolved": [],
    }

    merged = _merge_fragment_truth(whole, fragments)

    assert merged["main_view"]["keyways"] == [verified_keyway]
    assert "cross_holes" not in merged["main_view"]
    assert "chamfers" not in merged["main_view"]


def test_unverified_fragment_outer_wins_when_it_found_more_steps_than_whole():
    """Live-reproduced 2026-08-14 on example-drawings/shaft_detail.png: all 3
    fragment consensus passes read 3 outer steps (Ø30/Ø50/Ø30) identically —
    correct — but none of them carry "evidence" (_ROTATION_PROMPT's own
    schema has no such field, only outer_sections_from_diameter_evidence's
    output ever does), so verified_outer was always False for this path and
    the independently-re-read whole-sheet fallback's WORSE 2-step read
    (it merged two steps into one) silently won every time. A fragment
    profile with strictly more steps than whole-sheet found must not be
    discarded just because it lacks formal evidence."""
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragment_outer = [
        {"diameter_mm": 30, "length_mm": 220, "note": "h6"},
        {"diameter_mm": 50, "length_mm": 840, "note": "h6"},
        {"diameter_mm": 30, "length_mm": 220, "note": "k6"},
    ]
    fragments = {"main_view": {"outer": fragment_outer}, "unresolved": []}
    whole = {
        "main_view": {"outer": [
            {"diameter_mm": 50, "length_mm": 220, "note": "ступень Ø50h6"},
            {"diameter_mm": 30, "length_mm": 840, "note": "ступень Ø30k6, Ø30h6"},
        ]},
        "unresolved": [],
    }

    merged = _merge_fragment_truth(whole, fragments)

    assert merged["main_view"]["outer"] == fragment_outer


def test_verified_or_more_complete_outer_still_loses_to_a_richer_whole_read():
    """The other direction must still hold: an unverified fragment profile
    with FEWER (or equal) steps than whole-sheet found is not preferred —
    only "found strictly more" is trusted, not "found something different"."""
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragments = {
        "main_view": {"outer": [{"diameter_mm": 40, "length_mm": 100}]},
        "unresolved": [],
    }
    whole_outer = [
        {"diameter_mm": 40, "length_mm": 60},
        {"diameter_mm": 30, "length_mm": 40},
    ]
    whole = {"main_view": {"outer": whole_outer}, "unresolved": []}

    merged = _merge_fragment_truth(whole, fragments)

    assert merged["main_view"]["outer"] == whole_outer


def test_unverified_fragment_feature_fills_a_gap_whole_left_empty():
    """Same reasoning as the outer[] fix, applied to feature_fields:
    _FEATURES_PROMPT's schema has no evidence field either, so a correctly
    read keyway/groove/cross_hole with no evidence used to be discarded
    even when whole-sheet's own field for it was simply empty — the one
    case where there is nothing to lose by using it."""
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragment_groove = [{"axial_position_mm": 12, "width_mm": 6, "depth_mm": 1}]
    fragments = {
        "main_view": {"grooves": fragment_groove},
        "unresolved": [],
    }
    whole = {"main_view": {}, "unresolved": []}

    merged = _merge_fragment_truth(whole, fragments)

    assert merged["main_view"]["grooves"] == fragment_groove


def test_unverified_fragment_feature_does_not_overwrite_whole_own_read():
    """The gap-filling above must not become overwriting: when whole-sheet
    already found something for that field, an unverified fragment answer
    must not silently replace it."""
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragments = {
        "main_view": {"grooves": [{"axial_position_mm": 12, "width_mm": 6, "depth_mm": 1}]},
        "unresolved": [],
    }
    whole_grooves = [{"axial_position_mm": 99, "width_mm": 3, "depth_mm": 2}]
    whole = {"main_view": {"grooves": whole_grooves}, "unresolved": []}

    merged = _merge_fragment_truth(whole, fragments)

    assert merged["main_view"]["grooves"] == whole_grooves


def test_named_feature_doubt_does_not_clear_unrelated_feature_fields():
    """Live-found bug (shaft_detail.png): a "малые элементы: ..." message
    naming ONE feature type used to clear ALL of them (chamfers/fillets/
    grooves/axial_holes/circular_hole_patterns too), silently discarding
    correctly-read data no unresolved message ever cast doubt on. Only the
    field(s) an unresolved message actually names should be cleared; an
    unrecognized message (no known feature keyword) still conservatively
    clears everything, same as before.
    """
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragments = {
        "main_view": {},
        # Real message text captured from a live read of
        # example-drawings/shaft_detail.png — only names cross_holes.
        "unresolved": [
            "малые элементы: поперечное отверстие Ø10 указано, но не локализовано",
        ],
    }
    whole = {
        "main_view": {
            "cross_holes": [{"diameter_mm": 10, "axial_position_mm": 400}],
            "keyways": [{"axial_start_mm": 305, "length_mm": 90, "width_mm": 12}],
            "chamfers": [{"size_mm": 2, "location": "left_end"}],
            "fillets": [{"radius_mm": 3, "location": "step_1"}],
        },
        "unresolved": [],
    }

    merged = _merge_fragment_truth(whole, fragments)

    assert "cross_holes" not in merged["main_view"]  # named -> doubted, cleared
    assert merged["main_view"]["keyways"] == whole["main_view"]["keyways"]
    assert merged["main_view"]["chamfers"] == whole["main_view"]["chamfers"]
    assert merged["main_view"]["fillets"] == whole["main_view"]["fillets"]


def test_evidence_blocker_message_is_attributed_to_keyways_not_everything():
    """The "evidence: {blocker}" construction (spec_fragments.py's keyway
    evidence-blocker list) reads like a generic complaint but is keyway-
    specific in origin — it should doubt keyways, not every feature field."""
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragments = {
        "main_view": {},
        "unresolved": [
            "малые элементы: evidence: геометрия не отделена от аннотаций по цвету",
        ],
    }
    whole = {
        "main_view": {
            "keyways": [{"axial_start_mm": 305, "length_mm": 90, "width_mm": 12}],
            "chamfers": [{"size_mm": 2, "location": "left_end"}],
        },
        "unresolved": [],
    }

    merged = _merge_fragment_truth(whole, fragments)

    assert "keyways" not in merged["main_view"]
    assert merged["main_view"]["chamfers"] == whole["main_view"]["chamfers"]


def test_unrecognized_small_feature_message_still_clears_everything():
    """A "малые элементы: ..." message naming no recognized feature keyword
    is the one case that still conservatively clears every feature_field —
    unchanged from before, since we genuinely can't tell which field it's
    about."""
    from app.ai.cad_recognize.spec_fragments import _merge_fragment_truth

    fragments = {
        "main_view": {},
        "unresolved": ["малые элементы: keyway-2: глубина не подтверждена"],
    }
    whole = {
        "main_view": {
            "keyways": [{"axial_start_mm": 10, "length_mm": 20}],
            "chamfers": [{"size_mm": 2, "location": "right_end"}],
        },
        "unresolved": [],
    }

    merged = _merge_fragment_truth(whole, fragments)

    assert "keyways" not in merged["main_view"]
    assert "chamfers" not in merged["main_view"]


def test_feature_fields_cast_into_doubt_maps_real_captured_messages():
    """Regression-pins the keyword mapping against message text actually
    captured from a live shaft_detail.png read (not just synthetic
    fixtures) — see memory project_cad_shaft_detail_reader_gaps_2026_08_11."""
    from app.ai.cad_recognize.spec_fragments import _feature_fields_cast_into_doubt

    feature_fields = (
        "chamfers", "fillets", "grooves", "keyways", "cross_holes", "axial_holes",
        "circular_hole_patterns",
    )
    assert _feature_fields_cast_into_doubt(
        "малые элементы: evidence: геометрия не отделена от аннотаций по цвету",
        feature_fields,
    ) == ("keyways",)
    assert _feature_fields_cast_into_doubt(
        "малые элементы: поперечное отверстие Ø10 указано, но не локализовано",
        feature_fields,
    ) == ("cross_holes",)
    assert _feature_fields_cast_into_doubt(
        "малые элементы: осевое отверстие M8 не найдено", feature_fields,
    ) == ("axial_holes",)
    assert _feature_fields_cast_into_doubt(
        "малые элементы: неизвестная причина", feature_fields,
    ) == feature_fields


def test_flags_outer_diameter_the_model_invented():
    """Live-found on a real shaft drawing: the model's own callout pass
    correctly read Ø30h6/Ø50h6/Ø30k6 into `dimensions`, but the SAME
    response wrote Ø50/Ø70/Ø50 into main_view.outer -- inventing a Ø70 that
    appears nowhere on the sheet. outer/bore steps carry no evidence of
    their own, so nothing else in this file would have caught it."""
    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": 50.0, "length_mm": 220.0},
                {"diameter_mm": 70.0, "length_mm": 400.0},  # not on the sheet
                {"diameter_mm": 50.0, "length_mm": 220.0},
            ],
        },
        "dimensions": [
            {"value": "Ø50h6"}, {"value": "Ø30h6"}, {"value": "Ø30k6"},
            {"value": "220"}, {"value": "840"},
        ],
        "annotations": [],
        "unresolved": [],
    }

    result = _flag_unconfirmed_outer_bore_diameters(spec)

    assert result["unresolved"] == [
        "body:0:outer:1:diameter-unconfirmed: Ø70 не подтверждён ни одним "
        "размером в перечне на листе",
    ]


def test_does_not_flag_diameters_that_do_appear_on_the_sheet():
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 50.0, "length_mm": 220.0}],
            "bore": [{"diameter_mm": 10.03, "length_mm": 400.0}],  # 0.3% off -- within tolerance
        },
        "dimensions": [{"value": "Ø50h6"}, {"value": "Ø10H7"}],
        "annotations": [],
        "unresolved": [],
    }

    result = _flag_unconfirmed_outer_bore_diameters(spec)

    assert result["unresolved"] == []


def test_no_callouts_on_sheet_means_nothing_to_cross_check_against():
    """No Ø-marked value anywhere in dimensions/annotations -- can't tell an
    invented diameter from a real one, so nothing is flagged (fail-closed in
    the other direction: don't invent false positives either)."""
    spec = {
        "main_view": {"outer": [{"diameter_mm": 999.0, "length_mm": 1.0}]},
        "dimensions": [],
        "annotations": [],
        "unresolved": [],
    }

    result = _flag_unconfirmed_outer_bore_diameters(spec)

    assert result["unresolved"] == []


def test_callouts_split_by_the_sheets_own_diameter_mark():
    """A drawing already says which numbers are diameters: it marks them Ø.

    Pooling them with the lengths hands the reader a bag of numbers and lets
    it answer "Ø102" when asked for an axial position and "470" when asked for
    a diameter — which is exactly what every refusal on the spindle sheet
    looked like: 8 diameters against 6 axial values, an outer profile summing
    to 364 on a part 470 long.
    """
    from app.ai.cad_recognize.spec_fragments import _callout_numbers

    callouts = {
        "dimensions": [
            {"value": "Ø80js6"}, {"value": "Ø102h6"}, {"value": "Ø44H7"},
            {"value": "150"}, {"value": "240"}, {"value": "470"},
        ]
    }

    diameters = _callout_numbers(callouts, "diameter")
    lengths = _callout_numbers(callouts, "linear")

    assert set(diameters) == {102.0, 80.0, 44.0}
    assert set(lengths) == {470.0, 240.0, 150.0}
    # Nothing appears in both, and "all" still returns everything for the
    # checks that only need to know a number was somewhere on the sheet.
    assert not set(diameters) & set(lengths)
    assert set(_callout_numbers(callouts)) == set(diameters) | set(lengths)


def test_uppercase_hole_fit_is_a_diameter_when_ocr_lost_the_symbol():
    from app.ai.cad_recognize.spec_fragments import _callout_numbers

    callouts = {
        "dimensions": [
            {"value": "50H7 (+0,025)"},
            {"value": "470h14"},
        ]
    }

    assert _callout_numbers(callouts, "diameter") == [50.0]
    assert _callout_numbers(callouts, "linear") == [470.0]


def test_lowercase_shaft_fit_is_a_diameter_when_ocr_lost_the_symbol():
    from app.ai.cad_recognize.spec_fragments import _callout_numbers

    callouts = {
        "dimensions": [
            {"value": "50h7 -0.019 / -0.028"},
            {"value": "470 h14"},
        ]
    }

    assert _callout_numbers(callouts, "diameter") == [50.0]
    assert _callout_numbers(callouts, "linear") == [470.0]


def test_a_diameter_callout_extracts_the_number_next_to_the_mark_not_the_first_in_the_string():
    """Live-found on example-drawings/shaft_detail.png, 2026-08-14: a single
    combined technical-requirements line — "1. HRC 42...48 (шейки Ø30h6,
    Ø30k6)" — has an unrelated number (a hardness range) BEFORE its actual
    Ø-marked diameter in the same string. Taking the first number anywhere
    in a "marked" callout silently returned 42, which then fed a phantom
    Ø42 outer step through _flag_unconfirmed_outer_bore_diameters as if it
    were a sheet-confirmed diameter."""
    from app.ai.cad_recognize.spec_fragments import _callout_numbers

    callouts = {
        "dimensions": [
            {"value": "1. НRC 42...48 (шейки Ø30h6, Ø30k6)"},
        ]
    }

    assert _callout_numbers(callouts, "diameter") == [30.0]


def test_free_text_splits_two_diameters_sharing_one_bullet():
    """Live-found 2026-08-15: a free-form "describe everything" answer from a
    thinking-capable model regularly puts two diameters in one bullet —
    "...коническая поверхность φ56,55 (малый) и φ80 js6 ... (большой)." —
    unlike _CALLOUT_PROMPT's one-value-per-dimensions[]-entry answer. Naively
    wrapping the whole bullet as one entry would lose the second diameter,
    since _callout_numbers's diameter branch only reads the first number
    after the mark in an item."""
    from app.ai.cad_recognize.spec_fragments import (
        _callout_numbers,
        _numbers_from_free_text,
    )

    text = (
        "Коническая поверхность с конусностью 7:24; диаметры конуса "
        "φ56,55 (малый) и φ80 js6 (+0,0095 / −0,0095) (большой)."
    )
    entries = _numbers_from_free_text(text)
    callouts = {"dimensions": [{"value": entry} for entry in entries]}

    diameters = set(_callout_numbers(callouts, "diameter"))
    assert {56.55, 80.0} <= diameters


def test_free_text_reads_a_bare_fit_code():
    from app.ai.cad_recognize.spec_fragments import (
        _callout_numbers,
        _numbers_from_free_text,
    )

    text = "Наружный диаметр φ102 h6. Резьба на торце: M75×1,5. Участок 50h7."
    entries = _numbers_from_free_text(text)
    callouts = {"dimensions": [{"value": entry} for entry in entries]}

    diameters = set(_callout_numbers(callouts, "diameter"))
    assert {102.0, 50.0} <= diameters


def test_free_text_does_not_derive_a_diameter_from_a_thread_nominal():
    """Live-verified 2026-08-15 on the spindle sheet: deriving a synthetic
    Ø54,5 candidate from "M54,5x2" (an INTERNAL thread's nominal diameter)
    put it 0.9% from the sheet's real Ø55 bore step. Two candidates that
    close made ``_contour_bore_observations``'s per-pixel "nearest known
    value" snap oscillate between them, fragmenting one confirmable Ø55/25mm
    bore plateau into zero bore sections — turning a correctly-readable
    sheet into an emptied-out one. A thread's nominal is by definition close
    to its shaft/hole's real diameter, so deriving one from the other is
    exactly the mechanism most likely to create this collision; the token
    must survive as text (thread-carrier matching still wants it) without
    also becoming a second, almost-identical diameter candidate."""
    from app.ai.cad_recognize.spec_fragments import (
        _callout_numbers,
        _numbers_from_free_text,
    )

    text = "Резьба M54,5×2 на правом торце, глубина 25."
    entries = _numbers_from_free_text(text)
    callouts = {"dimensions": [{"value": entry} for entry in entries]}

    assert 54.5 not in set(_callout_numbers(callouts, "diameter"))
    # The designation itself is not lost — it travels as part of its line.
    assert any("54,5" in entry or "54.5" in entry for entry in entries)


def test_free_text_keeps_noise_words_so_existing_filters_still_apply():
    """A bare number extracted without its sentence would defeat
    _callout_numbers's own noise filters (HRC ranges, hole/chamfer counts,
    angles) — those key on words like "отв."/"фасок"/"HRC" that only exist
    if the line survives whole, not as an isolated digit."""
    from app.ai.cad_recognize.spec_fragments import (
        _callout_numbers,
        _numbers_from_free_text,
    )

    text = "Твёрдость HRC 58...62. 6 фасок на торце. 12 отв. φ4 по окружности."
    entries = _numbers_from_free_text(text)
    callouts = {"dimensions": [{"value": entry} for entry in entries]}

    lengths = set(_callout_numbers(callouts, "linear"))
    assert 6.0 not in lengths
    assert 12.0 not in lengths
    assert 58.0 not in lengths


def test_free_text_drops_gost_reference_numbers():
    from app.ai.cad_recognize.spec_fragments import _numbers_from_free_text

    text = "Материал: сталь 55, ГОСТ 1050-2013. Общая длина 470 мм."
    entries = _numbers_from_free_text(text)

    joined = " ".join(entries)
    assert "1050" not in joined
    assert "2013" not in joined
    assert "470" in joined
