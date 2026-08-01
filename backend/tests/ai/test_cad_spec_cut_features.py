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

from pathlib import Path

import pytest
from PIL import Image

from app.ai.cad_recognize.spec_fragments import _read_cut_features
from app.ai.cad_recognize.spec_fragments import _feature_completeness_issues
from app.ai.cad_recognize.spec_fragments import _assign_profile_threads
from app.ai.cad_recognize.spec_fragments import _recover_external_thread_carrier
from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

_OUTER = [
    {"diameter_mm": 80.0, "length_mm": 150.0},
    {"diameter_mm": 102.0, "length_mm": 200.0},
    {"diameter_mm": 60.0, "length_mm": 120.0},
]  # 470 mm long, biggest radius 51 mm


def test_spec_keeps_source_visible_axial_pattern_with_unknown_build_fields():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {
            "type": "тело вращения",
            "outer": [{"diameter_mm": 80, "length_mm": 470}],
            "axial_holes": [{
                "count": 2,
                "bolt_circle_diameter_mm": 65,
                "start_angle_deg": 90,
                "spacing_deg": 180,
                "from_face": None,
                "through": None,
                "pilot_diameter_mm": None,
                "thread": {
                    "designation": "M8",
                    "nominal_diameter_mm": 8,
                    "internal": True,
                },
            }],
        }
    })
    pattern = spec.main_view.axial_holes[0]
    assert pattern.from_face is None
    assert pattern.through is None


def test_metric_nominal_is_parsed_from_partial_axial_thread_designation():
    raw = _coerce_spec_containers({
        "main_view": {
            "type": "тело вращения",
            "outer": [{"diameter_mm": 80, "length_mm": 470}],
            "axial_holes": {
                "count": 2,
                "bolt_circle_diameter_mm": 65,
                "thread": {"designation": "М8", "internal": True},
            },
        }
    })
    spec = EngineeringDrawingSpec.model_validate(raw)
    assert spec.main_view.axial_holes[0].thread.nominal_diameter_mm == 8


async def _read(monkeypatch, answer: dict) -> dict:
    async def fake_ask(*_a, **_kw):
        return answer

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    return await _read_cut_features(
        object(), _OUTER, router=object(), confidential=True
    )


@pytest.mark.asyncio
async def test_feature_question_is_kept_in_the_parent_reader_audit(monkeypatch):
    audit: list[dict] = []

    async def fake_ask(*_a, **kwargs):
        kwargs["audit"].append({
            "question": "cut features",
            "model": "test-reader",
            "raw_response": '{"chamfers": []}',
        })
        return {"chamfers": []}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    await _read_cut_features(
        object(), _OUTER, router=object(), confidential=True, audit=audit
    )

    assert audit == [{
        "question": "cut features",
        "model": "test-reader",
        "raw_response": '{"chamfers": []}',
    }]


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


@pytest.mark.asyncio
async def test_local_slot_depth_cannot_be_replaced_by_unrelated_sheet_number(monkeypatch):
    from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions

    source = (
        Path(__file__).resolve().parents[3]
        / "test_vector_files" / "detal_126.png"
    )
    image = Image.open(source).convert("RGB")
    values = [470, 270, 240, 150, 99, 85, 78, 50, 35, 26, 25, 20, 18, 15, 14, 12, 8, 5, 4, 3.2]
    profile_evidence = {
        "axial_map": localize_axial_dimensions(image, values),
        "diameter_map": {"profile_center_y_px": 593},
    }

    async def fake_ask(*_args, **_kwargs):
        return {
            "keyways": [
                {"axial_start_mm": 277, "length_mm": 85, "width_mm": 12, "depth_mm": 3.2},
                {"axial_start_mm": 372, "length_mm": 35, "width_mm": 8, "depth_mm": 4},
            ]
        }

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    result = await _read_cut_features(
        image,
        [{"diameter_mm": 80, "length_mm": 470}],
        router=object(),
        confidential=True,
        source_image=image,
        callouts={"dimensions": [{"value": str(value)} for value in values]},
        profile_evidence=profile_evidence,
    )

    assert "keyways" not in result
    blockers = profile_evidence["feature_unresolved"]
    assert "3.2" in blockers[0]
    assert "keyway-2" in blockers[1]


@pytest.mark.asyncio
async def test_end_view_pattern_is_recorded_without_inventing_hole_depth(monkeypatch):
    from app.ai.cad_recognize.axial_dimensions import localize_axial_dimensions

    source = Path(__file__).resolve().parents[3] / "test_vector_files" / "detal_126.png"
    image = Image.open(source).convert("RGB")
    linear = [470, 270, 240, 150, 99, 85, 78, 50, 35, 26, 25, 20, 18, 15, 14, 12, 8, 5, 4, 3]
    profile_evidence = {
        "axial_map": localize_axial_dimensions(image, linear),
        "diameter_map": {
            "profile_center_y_px": 593,
            "observations": [{"role": "outer", "value_mm": 80}],
        },
    }

    async def fake_ask(prompt, *_args, **_kwargs):
        if "ТОЛЬКО мелкие элементы" in prompt:
            return {
                "chamfers": [
                    {"size_mm": 1, "angle_deg": 45, "location": "left_end"}
                ]
            }
        return {}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    result = await _read_cut_features(
        image,
        [{"diameter_mm": 80, "length_mm": 470}],
        router=object(),
        confidential=True,
        source_image=image,
        callouts={"dimensions": [
            *({"value": str(value)} for value in linear),
            {"value": "Ø65"},
            {"value": "2 отв. M8"},
            {"value": "6 фасок 1×45°"},
        ]},
        profile_evidence=profile_evidence,
    )

    pattern, = result["axial_holes"]
    assert pattern["count"] == 2
    assert pattern["bolt_circle_diameter_mm"] == 65
    assert pattern["thread"]["designation"] == "M8"
    assert pattern["from_face"] is None
    assert pattern["through"] is None
    assert pattern["pilot_diameter_mm"] is None
    assert pattern["evidence"][0]["bbox"]
    assert result["chamfers"][0]["evidence"][0]["bbox"]
    blockers = "\n".join(profile_evidence["feature_unresolved"])
    assert "несущий участок" not in blockers
    assert "осевые отверстия M8" in blockers
    assert "указано 6 фасок, локализовано 1" in blockers


def test_named_holes_chamfers_and_threads_cannot_disappear():
    issues = _feature_completeness_issues(
        {
            "dimensions": [
                {"value": "Ø14 (+0,02)"},
                {"value": "Ø10 ±0,05"},
                {"value": "Ø9"},
                {"value": "1×45°"},
                {"value": "6 фасок"},
                {"value": "M75×1,5"},
                {"value": "M54,5×2"},
            ]
        },
        {
            "chamfers": [{"size_mm": 1, "location": "left_end"}],
            "cross_holes": [{"diameter_mm": 9, "axial_position_mm": 455}],
        },
        [{"diameter_mm": 80, "length_mm": 470}],
        {"observations": [{"role": "outer", "value_mm": 80}]},
    )

    assert "поперечное отверстие Ø9" not in "\n".join(issues)
    assert "поперечное отверстие Ø10" in "\n".join(issues)
    assert "поперечное отверстие Ø14" in "\n".join(issues)
    assert "указано 6 фасок, локализовано 1" in issues
    assert any("M54,5" in issue and "M75" in issue for issue in issues)


def test_keyway_widths_with_no_radial_label_are_not_cross_holes():
    issues = _feature_completeness_issues(
        {"dimensions": [
            {"value": "Ø12"}, {"value": "Ø8"}, {"value": "Ø14"},
        ]},
        {"cross_holes": [{"diameter_mm": 14, "axial_position_mm": 410}]},
        [{"diameter_mm": 72, "length_mm": 470}],
        {"observations": [{"role": "outer", "value_mm": 72}]},
        feature_evidence={
            "radial_opening_candidates": [{"id": "radial-opening-1"}],
            "diameter_label_observations": [{"value_mm": 14}],
        },
    )

    assert not any("Ø12" in issue or "Ø8" in issue for issue in issues)


@pytest.mark.asyncio
async def test_radial_crop_maps_only_named_diameters_to_known_candidates(monkeypatch):
    source = Image.new("RGB", (900, 700), "white")
    evidence = {
        "status": "ok",
        "keyway_candidates": [],
        "radial_opening_candidates": [
            {
                "id": "radial-opening-1",
                "bbox": [400, 150, 440, 550],
                "axial_position_mm": 410,
                "supported_diameters_mm": [14],
            },
            {
                "id": "radial-opening-2",
                "bbox": [470, 150, 510, 550],
                "axial_position_mm": 430,
                "supported_diameters_mm": [14],
            },
        ],
        "blockers": [],
    }
    monkeypatch.setattr(
        "app.ai.cad_recognize.turned_features.localize_turned_features",
        lambda *_args, **_kwargs: evidence,
    )

    async def fake_ask(prompt, *_args, **_kwargs):
        if "увеличенный правый узел" in prompt:
            return {"radial_features": [
                {
                    "candidate_id": "radial-opening-2",
                    "diameter_mm": 24,
                    "kind": "counterbore",
                    "side": "top",
                },
                {
                    "candidate_id": "invented-axis",
                    "diameter_mm": 14,
                    "kind": "through",
                    "side": "both",
                },
                {
                    "candidate_id": "radial-opening-1",
                    "diameter_mm": 999,
                    "kind": "through",
                    "side": "both",
                },
            ]}
        return {}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    profile_evidence = {
        "axial_map": {"overall_mm": 470, "datum_line": [100, 800]},
        "diameter_map": {"profile_center_y_px": 350, "observations": []},
    }
    await _read_cut_features(
        source,
        [{"diameter_mm": 80, "length_mm": 470}],
        router=object(),
        confidential=True,
        source_image=source,
        callouts={"dimensions": [
            {"value": "Ø14"}, {"value": "Ø24"}, {"value": "Ø80"},
        ]},
        profile_evidence=profile_evidence,
    )

    assert profile_evidence["radial_hypotheses"] == [{
        "candidate_id": "radial-opening-2",
        "diameter_mm": 24,
        "kind": "counterbore",
        "side": "top",
        "source_crop_bbox": [240, 0, 770, 680],
    }]


@pytest.mark.asyncio
async def test_opposite_to_bore_holes_get_geometry_derived_depth(monkeypatch):
    source = Image.new("RGB", (900, 700), "white")
    evidence = {
        "status": "ok",
        "keyway_candidates": [],
        "radial_opening_candidates": [{
            "id": "radial-opening-2",
            "bbox": [500, 150, 540, 550],
            "axial_position_mm": 430,
            "supported_diameters_mm": [14],
        }, {
            "id": "radial-opening-3",
            "bbox": [600, 150, 630, 550],
            "axial_position_mm": 455,
            "supported_diameters_mm": [9, 10],
        }],
        "diameter_label_observations": [
            {"value_mm": 24, "side": "top", "bbox": [710, 150, 760, 180], "raw_text": "Ø24"},
            {"value_mm": 10, "side": "top", "bbox": [700, 200, 750, 230], "raw_text": "Ø10"},
            {"value_mm": 9, "side": "bottom", "bbox": [700, 500, 750, 530], "raw_text": "Ø9"},
        ],
        "blockers": [
            "radial-opening-3: контур допускает несколько диаметров Ø9/Ø10"
        ],
    }
    monkeypatch.setattr(
        "app.ai.cad_recognize.turned_features.localize_turned_features",
        lambda *_args, **_kwargs: evidence,
    )

    async def fake_ask(prompt, *_args, **_kwargs):
        if "увеличенный правый узел" in prompt:
            return {"radial_features": [
                {"candidate_id": "radial-opening-3", "diameter_mm": 10,
                 "kind": "to_bore", "side": "bottom"},
                {"candidate_id": "radial-opening-3", "diameter_mm": 24,
                 "kind": "counterbore", "side": "top", "depth_mm": 3},
                {"candidate_id": "radial-opening-2", "diameter_mm": 9,
                 "kind": "to_bore", "side": "bottom"},
            ]}
        return {}

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    profile_evidence = {
        "axial_map": {"overall_mm": 470, "datum_line": [100, 800]},
        "diameter_map": {
            "profile_center_y_px": 350,
            "observations": [
                {"role": "outer", "source": "vector_contour", "value_mm": 72,
                 "axial_interval_mm": [400, 470]},
                {"role": "bore", "source": "vector_contour", "value_mm": 55,
                 "axial_interval_mm": [445, 469]},
            ],
        },
    }
    result = await _read_cut_features(
        source,
        [{"diameter_mm": 72, "length_mm": 470}],
        router=object(), confidential=True, source_image=source,
        callouts={"dimensions": [
            {"value": "Ø24"}, {"value": "Ø10"}, {"value": "Ø9"},
        ]},
        profile_evidence=profile_evidence,
    )

    assert [
        (item["diameter_mm"], item["angle_deg"], item["depth_mm"])
        for item in result["cross_holes"]
    ] == [(10.0, 0.0, 8.5), (9.0, 180.0, 8.5)]
    assert result["cross_holes"][0]["counterbore_diameter_mm"] == 24.0
    assert result["cross_holes"][0]["counterbore_depth_mm"] == 3.0
    assert not any(
        "несколько диаметров" in item
        for item in profile_evidence["feature_unresolved"]
    )


def test_thread_is_assigned_only_to_a_unique_matching_profile_section():
    outer = [{"diameter_mm": 80, "length_mm": 400}, {"diameter_mm": 72, "length_mm": 70}]
    bore = [{"diameter_mm": 56, "length_mm": 445}, {"diameter_mm": 55, "length_mm": 25}]

    unresolved = _assign_profile_threads(
        {"dimensions": [{"value": "M75×1,5"}, {"value": "M54,5×2"}]},
        outer,
        bore,
    )

    assert unresolved == ["M75x1,5"]
    assert bore[1]["thread"] == {
        "designation": "M54,5x2",
        "system": "metric",
        "nominal_diameter_mm": 54.5,
        "pitch_mm": 2.0,
        "length_mm": 25.0,
        "internal": True,
        "evidence": [{"image_index": 0, "bbox": None, "raw_text": "M54,5×2"}],
    }
    issues = _feature_completeness_issues(
        {"dimensions": [{"value": "M75×1,5"}, {"value": "M54,5×2"}]},
        {},
        outer,
        {"observations": []},
        bore=bore,
    )
    assert len(issues) == 1
    assert "M75" in issues[0]
    assert "M54,5" not in issues[0]


def test_unassigned_thread_reports_measured_but_unbounded_carrier():
    issues = _feature_completeness_issues(
        {"dimensions": [{"value": "M75×1,5"}]},
        {},
        [{"diameter_mm": 72, "length_mm": 99}],
        {"outer_candidates": [{
            "value_mm": 75,
            "axial_interval_mm": [366.0, 400.0],
        }]},
    )

    assert len(issues) == 1
    assert "контур-кандидат Ø75" in issues[0]
    assert "366…400 мм" in issues[0]
    assert "двум осевым размерам" in issues[0]


@pytest.mark.asyncio
async def test_external_thread_carrier_requires_exact_chain_bounds(monkeypatch):
    async def fake_ask(*_args, **_kwargs):
        return {
            "confirmed": True,
            "start_mm": 377,
            "end_mm": 395,
            "length_mm": 18,
        }

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    outer = [
        {"diameter_mm": 102, "length_mm": 14, "evidence": [{"bbox": [0, 0, 1, 1]}]},
        {"diameter_mm": 80, "length_mm": 357, "evidence": [{"bbox": [1, 0, 2, 1]}]},
        {"diameter_mm": 72, "length_mm": 99, "evidence": [{"bbox": [2, 0, 3, 1]}]},
    ]
    bore = [
        {"diameter_mm": 56.55, "length_mm": 78},
        {"diameter_mm": 51, "length_mm": 72},
        {"diameter_mm": 44, "length_mm": 50},
        {"diameter_mm": 51, "length_mm": 152},
        {"diameter_mm": 50, "length_mm": 43},
        {"diameter_mm": 56, "length_mm": 50},
        {"diameter_mm": 55, "length_mm": 25},
    ]
    evidence = {
        "profile_center_y_px": 300,
        "outer_candidates": [{
            "value_mm": 75,
            "axial_interval_mm": [365.5, 401.2],
            "profile_interval_px": [520, 625],
        }],
    }
    callouts = {"dimensions": [
        {"value": "M75×1,5"}, {"value": "M75×1,5"}, {"value": "M8"},
        {"value": "25"}, {"value": "18"},
        {"value": "50"}, {"value": "99"},
    ]}

    recovered = await _recover_external_thread_carrier(
        Image.new("RGB", (900, 700), "white"),
        callouts,
        outer,
        bore,
        evidence,
        router=object(),
        confidential=True,
    )

    assert recovered is True
    assert [
        (item["diameter_mm"], item["length_mm"]) for item in outer
    ] == [(102, 14), (80, 357), (72, 6.0), (75.0, 18.0), (72, 75.0)]
    assert outer[3]["thread"]["designation"] == "M75x1,5"
    assert outer[3]["thread"]["length_mm"] == 18.0
    assert outer[3]["thread"]["internal"] is False


@pytest.mark.asyncio
async def test_external_thread_carrier_rejects_approximate_pixel_bounds(monkeypatch):
    async def fake_ask(*_args, **_kwargs):
        return {
            "confirmed": True,
            "start_mm": 365.5,
            "end_mm": 401.2,
            "length_mm": 35.7,
        }

    monkeypatch.setattr("app.ai.cad_recognize.spec_fragments._ask", fake_ask)
    outer = [{"diameter_mm": 72, "length_mm": 470}]
    original = [dict(outer[0])]
    recovered = await _recover_external_thread_carrier(
        Image.new("RGB", (900, 700), "white"),
        {"dimensions": [{"value": "M75×1,5"}, {"value": "18"}]},
        outer,
        [],
        {
            "profile_center_y_px": 300,
            "outer_candidates": [{
                "value_mm": 75,
                "axial_interval_mm": [365.5, 401.2],
                "profile_interval_px": [520, 625],
            }],
        },
        router=object(),
        confidential=True,
    )

    assert recovered is False
    assert outer == original
