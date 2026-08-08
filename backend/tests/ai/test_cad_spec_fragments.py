"""Fragment reading: narrow questions, isolated failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ai.cad_recognize.spec_fragments import (
    _clean_callout_observations,
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
    # The expensive whole-sheet read must not run when it is not needed — but
    # the fragment read itself now runs once per pass, because a single read is
    # a single bet and this pipeline claims agreement, not luck.
    assert called == ["fragments"] * 3
    assert result["consensus"]["usable"] == 3
    # Three identical reads are not a disagreement about anything.
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
