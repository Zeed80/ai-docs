"""The sheet built from the solid: what goes on it, and where.

No kernel here — the projections are recorded fixtures shaped exactly like
``/drawing`` answers, so these tests are about the decisions: which views a part
gets, which sheet and scale, which edges become dimensions, and what the drawn
result claims about itself. The live half lives in scripts/cad_kernel_smoke.py.
"""

from __future__ import annotations

import io

import ezdxf
import pytest

from app.ai.cad_ir.sheet_from_solid import (
    _assemble,
    _dimension_requests,
    _label_dimensions,
    classify_part,
    plan_sheet,
    plan_views,
    verify_view_coverage,
)

_SHAFT = {
    "part": "Вал",
    "main_view": {
        "type": "тело вращения (вал)",
        "outer": [
            {"diameter_mm": 80.0, "length_mm": 150.0},
            {"diameter_mm": 102.0, "length_mm": 200.0},
            {"diameter_mm": 60.0, "length_mm": 120.0},
        ],
    },
    "dimensions": [
        {"value": "Ø80js6"},
        {"value": "Ø102"},
        {"value": "Ø60"},
        {"value": "150"},
        {"value": "120"},
        {"value": "470"},
    ],
}
_HOLLOW = {
    **_SHAFT,
    "main_view": {**_SHAFT["main_view"], "bore": [{"diameter_mm": 40.0, "length_mm": 470.0}]},
}
_FLANGE = {
    "part": "Фланец",
    "main_view": {
        "type": "фланец",
        "profile": {"shape": "circle", "diameter_mm": 560.0, "thickness_mm": 20.0},
    },
}
_SHAFT_REPORT = {"bounds_mm": {"x": 102.0, "y": 102.0, "z": 470.0}}
_FLANGE_REPORT = {"bounds_mm": {"x": 560.0, "y": 560.0, "z": 20.0}}


def test_the_class_follows_the_data_not_the_label():
    """A flange read perfectly has come back labelled "тело вращения"."""
    assert classify_part(_SHAFT, _SHAFT_REPORT) == "solid_rotation"
    assert classify_part(_HOLLOW, _SHAFT_REPORT) == "hollow_rotation"
    assert classify_part(_FLANGE, _FLANGE_REPORT) == "flange"

    mislabelled = {
        "main_view": {
            "type": "тело вращения",
            "profile": {"shape": "circle", "diameter_mm": 560.0, "thickness_mm": 20.0},
        }
    }
    assert classify_part(mislabelled, _FLANGE_REPORT) == "flange"


def test_a_hollow_part_is_shown_in_section_and_only_once():
    """The section IS the main view (ГОСТ 2.305); the outline beside it would
    be the same body drawn twice."""
    plan = plan_sheet(_HOLLOW, _SHAFT_REPORT)
    kinds = [view["kind"] for view in plan.views]
    assert kinds[0] == "front"  # the kernel needs a base view to cut
    assert "section" in kinds
    # ...and that base view is scaffolding, not something the sheet carries.
    assert plan.scaffold_views == {0}


def test_a_solid_shaft_keeps_its_plain_view():
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    assert plan.scaffold_views == set()
    assert [view["kind"] for view in plan.views] == ["front"]


def test_a_view_the_reader_saw_is_reproduced():
    """ "top" was read, validated, and then silently never drawn."""
    spec = {**_SHAFT, "views": [{"kind": "top", "body_index": 0}]}
    assert "top" in [view["kind"] for view in plan_views("solid_rotation", spec)]


def test_read_offset_section_reaches_the_kernel_view_plan():
    spec = {
        **_SHAFT,
        "views": [
            {
                "kind": "section",
                "view_id": "section-b",
                "parent_view_id": "main",
                "label": "Б-Б",
                "section_origin_mm": 12.0,
                "section_path_mm": [[0, 0, 0], [20, 0, 0], [20, 10, 0]],
            }
        ],
    }
    section = next(view for view in plan_views("solid_rotation", spec) if view["kind"] == "section")
    assert section["label"] == "Б-Б"
    assert section["section_origin_mm"] == 12.0
    assert len(section["section_path_mm"]) == 3


def test_removed_section_is_built_as_section_with_separate_presentation_kind():
    spec = {
        **_SHAFT,
        "views": [
            {
                "kind": "removed_section",
                "view_id": "section-v",
                "parent_view_id": "main",
                "label": "В-В",
            }
        ],
    }
    plan = plan_sheet(spec, _SHAFT_REPORT)
    coverage = verify_view_coverage(plan, spec)
    removed = next(
        view for view in plan.views if view.get("presentation_kind") == "removed_section"
    )
    assert removed["kind"] == "section"
    assert removed["label"] == "В-В"
    assert coverage["ok"] is True


def test_detail_view_stays_an_explicit_coverage_blocker():
    spec = {
        **_SHAFT,
        "views": [{"kind": "detail", "view_id": "d", "label": "А"}],
    }
    coverage = verify_view_coverage(plan_sheet(spec, _SHAFT_REPORT), spec)
    assert coverage["ok"] is False
    assert coverage["missing"][0]["view"] == "detail"


def test_detail_with_model_crop_is_planned_and_satisfies_coverage():
    spec = {
        **_SHAFT,
        "views": [
            {
                "kind": "detail",
                "view_id": "d",
                "label": "А",
                "parent_view_id": "main",
                "detail_center_mm": [150.0, 0.0],
                "detail_radius_mm": 20.0,
                "detail_scale_factor": 4.0,
            }
        ],
    }
    plan = plan_sheet(spec, _SHAFT_REPORT)
    detail = next(view for view in plan.views if view["kind"] == "detail")

    assert detail["detail_center_mm"] == [150.0, 0.0]
    assert detail["detail_radius_mm"] == 20.0
    assert detail["detail_scale_factor"] == 4.0
    assert verify_view_coverage(plan, spec)["ok"] is True
    reason = next(item for item in plan.view_reasons if item["kind"] == "detail")
    assert "местный" in reason["reason"]


def test_view_plan_explains_why_each_projection_exists():
    spec = {
        "main_view": {
            **_SHAFT["main_view"],
            "cross_holes": [{"diameter_mm": 8, "axial_position_mm": 20}],
        }
    }
    plan = plan_sheet(spec, _SHAFT_REPORT)
    side = next(item for item in plan.view_reasons if item["kind"] == "side")
    assert "радиальных" in side["reason"]
    assert side["visible"] is True


def test_a_shaft_with_cross_features_gets_the_end_view_that_shows_them():
    spec = {
        "main_view": {
            **_SHAFT["main_view"],
            "keyways": [
                {"axial_start_mm": 20.0, "length_mm": 85.0, "width_mm": 12.0, "depth_mm": 5.0}
            ],
        }
    }
    assert "side" in [view["kind"] for view in plan_views("solid_rotation", spec)]
    plan = plan_sheet(spec, _SHAFT_REPORT)
    assert verify_view_coverage(plan, spec)["ok"] is True


def test_a_shaft_with_axial_holes_gets_the_end_view_that_shows_the_pattern():
    spec = {
        "main_view": {
            **_SHAFT["main_view"],
            "axial_holes": [
                {
                    "count": 2,
                    "bolt_circle_diameter_mm": 40,
                    "thread": {"designation": "M8", "nominal_diameter_mm": 8},
                }
            ],
        }
    }
    plan = plan_sheet(spec, _SHAFT_REPORT)
    assert "side" in [view["kind"] for view in plan.views]
    assert verify_view_coverage(plan, spec)["ok"] is True
    reason = next(item for item in plan.view_reasons if item["kind"] == "side")
    assert "осевых" in reason["reason"]


def test_view_coverage_requires_a_section_for_a_read_bore():
    plan = plan_sheet(_HOLLOW, _SHAFT_REPORT)
    assert verify_view_coverage(plan, _HOLLOW)["ok"] is True
    plan.views = [{"kind": "front"}]
    plan.scaffold_views = set()
    coverage = verify_view_coverage(plan, _HOLLOW)
    assert coverage["ok"] is False
    assert coverage["missing"][0]["view"] == "section"


def test_the_scale_is_standard_and_the_sheet_is_the_smallest_that_reads():
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    assert plan.scale_label in {"1:1", "1:2", "1:2.5", "1:4", "1:5"}
    assert plan.sheet_format in {"A4", "A3"}
    # A part is not enlarged onto a bigger sheet just because it would fit.
    assert plan.ratio <= 1.0


def test_redraw_sheet_is_geometry_only_by_default():
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    assert plan.geometry_only is True
    ir, _extent = _assemble({"views": [], "dimensions": []}, _SHAFT, plan)
    assert ir.sheet is not None
    assert ir.sheet.frame is False
    assert ir.sheet.title_block == {}
    assert not any("sheet_frame" in entity.evidence for entity in ir.entities)


def test_geometry_only_sheet_reopens_as_dxf_without_frame_entities():
    from app.ai.cad_ir.dxf_render import render_ir_to_dxf

    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    drawing = {
        "views": [
            {
                "kind": "front",
                "bounds_mm": {"u_min": 0, "u_max": 100, "v_min": -10, "v_max": 10},
                "visible": [{"type": "line", "points": [[0, 0], [100, 0]]}],
                "hidden": [],
                "hatch": [],
            }
        ],
        "dimensions": [],
    }
    ir, _extent = _assemble(drawing, _SHAFT, plan)
    document = ezdxf.read(io.StringIO(render_ir_to_dxf(ir).decode("utf-8")))
    entities = list(document.modelspace())
    assert entities
    assert all(entity.dxf.layer != "FRAME" for entity in entities)


def test_geometry_only_sheet_places_exact_structured_annotations_in_reserved_band():
    from app.ai.cad_ir.dxf_render import render_ir_to_dxf, verify_dxf_roundtrip

    spec = {
        **_SHAFT,
        "annotations": [
            {"kind": "roughness", "text": "Ra 1,6", "value": "1,6"},
            {"kind": "datum", "text": "Д", "symbol": "Д"},
            {
                "kind": "tolerance",
                "text": "↗ 0,008 Д",
                "symbol": "runout",
                "value": "0,008",
                "datum_refs": ["Д"],
            },
            # Technical notes do not belong on a geometry-only sheet.
            {"kind": "hardness", "text": "HRC 58…62"},
        ],
    }
    plan = plan_sheet(spec, _SHAFT_REPORT)
    ir, _extent = _assemble({"views": [], "dimensions": []}, spec, plan)

    annotations = [entity for entity in ir.entities if entity.type == "annotation"]
    assert [entity.text for entity in annotations] == ["Ra 1,6", "Д", "↗ 0,008 Д"]
    assert [entity.kind for entity in annotations] == ["roughness", "datum", "tolerance"]
    assert all(entity.assurance == "constraint_validated" for entity in annotations)
    assert len({entity.position.y for entity in annotations}) == 3
    assert verify_dxf_roundtrip(ir)["ok"] is True

    document = ezdxf.read(io.StringIO(render_ir_to_dxf(ir).decode("utf-8")))
    dxf_texts = {entity.dxf.text for entity in document.modelspace() if entity.dxftype() == "TEXT"}
    assert {"Ra 1,6", "Д", "↗ 0,008 Д"} <= dxf_texts


def test_the_source_scale_is_honoured_when_it_fits():
    spec = {**_SHAFT, "title_block": {"scale": "1:5"}}
    plan = plan_sheet(spec, _SHAFT_REPORT)
    assert plan.scale_label == "1:5"


def _view(kind: str, lines: list[tuple[int, float, float, float]], circles=()) -> dict:
    """A recorded /drawing view: (edge_index, v, u_from, u_to) per line."""
    return {
        "kind": kind,
        "bounds_mm": {"u_min": -94.0, "u_max": 94.0, "v_min": -20.4, "v_max": 20.4},
        "visible": [
            {
                "type": "line",
                "edge_index": index,
                "points": [[u0, v], [u1, v]],
            }
            for index, v, u0, u1 in lines
        ]
        + [
            {"type": "circle", "edge_index": index, "center": [0.0, 0.0], "radius": r}
            for index, r in circles
        ],
        "hidden": [],
        "hatch": [],
    }


# Measured off a real 1:2.5 section of the shaft above: each step's two
# generatrices, mirrored about the axis. Note the upper one comes SECOND for
# two of the three steps — the case that used to lose those diameters.
_SECTION = _view(
    "section",
    [
        (3, 16.0, -94.0, -34.0),
        (4, -16.0, -94.0, -34.0),
        (6, -20.4, -34.0, 46.0),
        (8, -12.0, 46.0, 94.0),
        (14, 12.0, 46.0, 94.0),
        (16, 20.4, -34.0, 46.0),
    ],
)


def test_every_step_diameter_is_dimensioned_whichever_generatrix_comes_first():
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    plan.ratio, plan.scaffold_views = 0.4, set()
    requests = _dimension_requests({"views": [_SECTION]}, _SHAFT, plan)

    diameters = sorted(request["_nominal_mm"] for request in requests if request["_is_diameter"])
    assert diameters == [60.0, 80.0, 102.0]
    # A diameter on a longitudinal view is measured between BOTH generatrices.
    assert all("second_edge_index" in request for request in requests if request["_is_diameter"])


def test_the_bore_is_dimensioned_too():
    plan = plan_sheet(_HOLLOW, _SHAFT_REPORT)
    plan.ratio, plan.scaffold_views = 0.4, set()
    section = _view(
        "section",
        [
            (3, 16.0, -94.0, -34.0),
            (4, -16.0, -94.0, -34.0),
            (10, -8.0, -94.0, 94.0),
            (11, 8.0, -94.0, 94.0),
        ],
    )
    requests = _dimension_requests({"views": [section]}, _HOLLOW, plan)
    assert 40.0 in [request["_nominal_mm"] for request in requests]


def test_the_dimension_chain_is_left_open():
    """ГОСТ 2.307 forbids closing it: the longest step goes undimensioned and
    the overall length carries it."""
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    plan.ratio, plan.scaffold_views = 0.4, set()
    lengths_view = _view(
        "front",
        [
            (1, 16.0, -94.0, -34.0),  # 150 mm step, at 1:2.5 -> 60 mm
            (5, 20.4, -34.0, 46.0),  # 200 mm step -> 80 mm (the longest: skipped)
            (6, 12.0, 46.0, 94.0),  # 120 mm step -> 48 mm
        ],
    )
    requests = _dimension_requests({"views": [lengths_view]}, _SHAFT, plan)
    lengths = sorted(request["_nominal_mm"] for request in requests if not request["_is_diameter"])
    assert 200.0 not in lengths
    assert lengths == [120.0, 150.0]


def test_overall_length_is_requested_only_once_between_end_faces():
    """A full-length bore edge must not duplicate the overall dimension."""
    spec = {
        "main_view": {
            "type": "тело вращения",
            "outer": [
                {"diameter_mm": 80.0, "length_mm": 40.0},
                {"diameter_mm": 60.0, "length_mm": 60.0},
            ],
            "bore": [{"diameter_mm": 30.0, "length_mm": 100.0}],
        }
    }
    plan = plan_sheet(spec, {"bounds_mm": {"x": 80, "y": 80, "z": 100}})
    plan.ratio, plan.scaffold_views = 1.0, set()
    view = _view(
        "section",
        [
            (1, 15.0, 0.0, 100.0),
            (2, 0.0, 0.0, 0.0),
            (3, 0.0, 100.0, 100.0),
        ],
    )

    requests = _dimension_requests({"views": [view]}, spec, plan)
    overall = [request for request in requests if request.get("_is_overall")]

    assert len(overall) == 1
    assert overall[0]["_nominal_mm"] == 100.0
    assert sum(request["_nominal_mm"] == 100.0 for request in requests) == 1


def test_a_dimension_on_a_scaffold_view_is_never_requested():
    """It would be placed on a view the sheet does not carry, and vanish."""
    plan = plan_sheet(_HOLLOW, _SHAFT_REPORT)
    plan.ratio = 0.4
    plan.scaffold_views = {0}
    requests = _dimension_requests({"views": [_SECTION]}, _SHAFT, plan)
    assert requests == []


def test_the_text_comes_from_the_sheet_and_the_value_from_the_model():
    dimensions = [{"kind": "DistanceY", "value_mm": 80.0, "label": ""}]
    _label_dimensions(dimensions, [{"_nominal_mm": 80.0, "_is_diameter": True}], _SHAFT)
    assert dimensions[0]["label"] == "Ø80js6"
    assert dimensions[0]["value_mm"] == 80.0


def test_a_measurement_that_contradicts_the_reading_is_not_labelled_with_it():
    """If the model says 79 where the sheet said Ø80js6, the drawing must not
    quietly show the reading over its own geometry."""
    dimensions = [{"kind": "DistanceY", "value_mm": 79.0, "label": ""}]
    _label_dimensions(dimensions, [{"_nominal_mm": 80.0, "_is_diameter": True}], _SHAFT)
    assert dimensions[0]["label"] == ""


def test_labels_follow_the_measurement_not_the_position_in_the_list():
    """The kernel drops dimensions it cannot place, so answers are NOT parallel
    to requests. Pairing by index slides every label one place along and puts a
    fit on the wrong feature."""
    # Requested Ø80, then 150, then Ø60 — but the kernel could not place the
    # first one, so only two come back.
    dimensions = [
        {"kind": "DistanceX", "value_mm": 150.0, "label": ""},
        {"kind": "DistanceY", "value_mm": 60.0, "label": ""},
    ]
    requests = [
        {"_nominal_mm": 80.0, "_is_diameter": True},
        {"_nominal_mm": 150.0, "_is_diameter": False},
        {"_nominal_mm": 60.0, "_is_diameter": True},
    ]
    _label_dimensions(dimensions, requests, _SHAFT)

    assert dimensions[0]["label"] == "150"
    assert dimensions[1]["label"] == "Ø60"


def test_a_callout_that_is_not_a_size_never_labels_a_dimension():
    """Found live on the spindle: "R4" matched a 4 mm step and was drawn as its
    LENGTH — a radius label sitting on a distance dimension. A thread, though,
    IS the diameter callout for its step: the sheet writes M75x1,5 exactly
    where it would otherwise write Ø75."""
    from app.ai.cad_recognize.spec_vectorize import _callout_kind, _read_dimension_index

    assert _callout_kind("R4") is None
    assert _callout_kind("1x45°") is None
    assert _callout_kind("Ra 6,3") is None
    assert _callout_kind("HRC 42...48") is None
    assert _callout_kind("150") == "linear"
    assert _callout_kind("Ø80js6") == "diameter"
    assert _callout_kind("M75x1,5") == "diameter"

    spec = {"dimensions": [{"value": "R4"}, {"value": "4"}, {"value": "M75x1,5"}]}
    index = _read_dimension_index(spec)
    assert [text for _value, text, _is_d in index] == ["4", "M75x1,5"]


# ── Простановка размеров на реалистичной детали (найдено генератором эталона) ─


def _lines(items):
    """(edge_index, (u0, v0), (u1, v1)) → рёбра вида, в том числе вертикальные."""
    return [
        {"type": "line", "edge_index": index, "points": [list(p0), list(p1)]}
        for index, p0, p1 in items
    ]


def test_a_diameter_is_found_when_a_keyway_splits_its_upper_generatrix():
    """Синтетический вал: Ø28 не был образмерен вовсе.

    Паз разрезает верхнюю образующую своей ступени на два куска, а пара
    образующих искалась только с ТОЧНО совпадающими концами — нижняя целая,
    верхние короче, пары нет. Диаметр — расстояние между линиями, их длина к
    нему не относится.
    """
    from app.ai.cad_ir.sheet_from_solid import _diameter_requests

    view = {
        "visible": _lines(
            [
                (1, (0.0, 14.0), (20.0, 14.0)),  # верх до паза
                (2, (50.0, 14.0), (70.0, 14.0)),  # верх после паза
                (3, (0.0, -14.0), (70.0, -14.0)),  # низ целиком
            ]
        )
    }
    requests = _diameter_requests(view, 0, [28.0], ratio=1.0)

    assert [request["_nominal_mm"] for request in requests] == [28.0]


def test_the_longest_shared_stretch_is_chosen_and_the_dimension_is_placed_in_it():
    """Первая подошедшая пара клала Ø28 поперёк паза — на короткий обрезок."""
    from app.ai.cad_ir.sheet_from_solid import _diameter_requests

    view = {
        "visible": _lines(
            [
                (1, (0.0, 14.0), (5.0, 14.0)),  # короткий обрезок у паза
                (2, (20.0, 14.0), (70.0, 14.0)),  # длинный чистый участок
                (3, (0.0, -14.0), (70.0, -14.0)),
            ]
        )
    }
    # Короткий обрезок стоит в списке ПЕРВЫМ — и всё равно не выигрывает.
    requests = _diameter_requests(view, 0, [28.0], ratio=1.0)

    assert requests[0]["edge_index"] == 2
    assert requests[0]["_place_u"] == 45.0


def test_step_lengths_are_measured_between_shoulders_not_by_edge_length():
    """Фаска укорачивает образующую крайней ступени: 15 мм читается как 14,x.

    Длина искалась совпадением длины ребра со спеком в пределах 1 %, и на
    синтетическом трёхступенчатом вале две длины из трёх остались без размера.
    Длина — расстояние вдоль оси между торцами поперёк неё.
    """
    from app.ai.cad_ir.sheet_from_solid import _step_length_requests

    outer = [{"d": 28.0, "l": 70.0}, {"d": 25.0, "l": 18.0}, {"d": 20.0, "l": 15.0}]
    view = {
        "visible": _lines(
            [
                (10, (0.0, -14.0), (0.0, 14.0)),  # левый торец
                (11, (70.0, 12.5), (70.0, 14.0)),  # уступ 28→25 (только кольцо)
                (12, (88.0, 10.0), (88.0, 12.5)),  # уступ 25→20
                (13, (103.0, -9.0), (103.0, 9.0)),  # правый торец за фаской
                (20, (88.0, 10.0), (102.0, 10.0)),  # образующая, укорочена фаской
            ]
        )
    }
    requests = _step_length_requests(view, 0, outer, [18.0, 15.0], ratio=1.0)

    assert sorted(request["_nominal_mm"] for request in requests) == [15.0, 18.0]
    assert all(request["kind"] == "DistanceX" for request in requests)


def test_a_groove_wall_next_to_a_shoulder_does_not_stand_in_for_it():
    """Стенка канавки у уступа — вертикаль в 0,8 мм от станции, не уступ."""
    from app.ai.cad_ir.sheet_from_solid import _step_length_requests

    outer = [{"d": 28.0, "l": 70.0}, {"d": 25.0, "l": 18.0}]
    view = {
        "visible": _lines(
            [
                (10, (0.0, -14.0), (0.0, 14.0)),
                (11, (70.8, 12.0), (70.8, 12.5)),  # стенка канавки, не уступ
                (12, (88.0, -12.5), (88.0, 12.5)),
            ]
        )
    }
    requests = _step_length_requests(view, 0, outer, [18.0], ratio=1.0)

    assert requests == []


# ── Пластины и фланцы (найдено генератором эталона) ─────────────────────────


def test_a_flat_part_is_drawn_in_plan_too_not_only_edge_on():
    """Фланец Ø250 с шестью отверстиями перечерчивался полоской 12 мм.

    Лист нёс главный вид и разрез — оба ребром. Вида вдоль оси выдавливания,
    где видны контур, отверстия и окружность болтов, не было вовсе.
    """
    assert any(view["kind"] == "side" for view in plan_views("flange", _FLANGE))
    assert any(view["kind"] == "side" for view in plan_views("plate", _FLANGE))


def test_a_flat_part_gets_its_outline_thickness_and_hole_sizes():
    """Простановка выходила сразу, если деталь не тело вращения: НИ ОДНОГО размера."""
    from app.ai.cad_ir.sheet_from_solid import _prismatic_dimension_requests

    spec = {
        "main_view": {
            "type": "пластина",
            "profile": {
                "shape": "rectangle",
                "width_mm": 60.0,
                "height_mm": 100.0,
                "thickness_mm": 16.0,
                "holes": [{"center_x_mm": 10.0, "center_y_mm": 5.0, "diameter_mm": 5.5}],
            },
        }
    }
    plan = plan_sheet(spec, {"bounds_mm": {"x": 60, "y": 100, "z": 16}})
    plan.ratio, plan.scaffold_views = 1.0, set()
    edge_view = {"visible": _lines([(1, (0, 0), (0, 60)), (2, (16, 0), (16, 60))])}
    plan_view = {
        "visible": [
            *_lines([(3, (0, 0), (0, 100)), (4, (60, 0), (60, 100))]),
            *_lines([(5, (0, 0), (60, 0)), (6, (0, 100), (60, 100))]),
            {"type": "circle", "edge_index": 7, "center": [40.0, 55.0], "radius": 2.75},
        ]
    }
    requests = _prismatic_dimension_requests([edge_view, plan_view], spec, plan)
    sizes = sorted(request["_nominal_mm"] for request in requests)

    assert sizes == [5.5, 16.0, 60.0, 100.0]


def test_each_size_is_dimensioned_once_per_sheet_not_once_per_view():
    """Толщина 16 вставала дважды, ширина 60 — трижды."""
    from app.ai.cad_ir.sheet_from_solid import _prismatic_dimension_requests

    spec = {
        "main_view": {
            "profile": {
                "shape": "rectangle",
                "width_mm": 60.0,
                "height_mm": 100.0,
                "thickness_mm": 16.0,
            }
        }
    }
    plan = plan_sheet(spec, {"bounds_mm": {"x": 60, "y": 100, "z": 16}})
    plan.ratio, plan.scaffold_views = 1.0, set()
    same = {"visible": _lines([(1, (0, 0), (0, 60)), (2, (16, 0), (16, 60))])}
    requests = _prismatic_dimension_requests([same, same, same], spec, plan)

    assert [request["_nominal_mm"] for request in requests].count(16.0) == 1


def test_a_diameter_techdraw_measured_as_zero_is_rebuilt_from_the_view_circle():
    """Headless TechDraw меряет Diameter на окружности как 0 — пропадали ВСЕ Ø фланца.

    Значение не выдумывается: радиус окружности ядро уже измерило при проекции.
    """
    from app.ai.cad_ir.sheet_from_solid import _diameters_from_circles

    plan = plan_sheet(_FLANGE, _FLANGE_REPORT)
    plan.ratio = 0.5
    drawing = {"dimensions": [{"view_index": 2, "kind": "Diameter", "value_mm": 0.0}]}
    requests = [
        {
            "view_index": 2,
            "kind": "Diameter",
            "_nominal_mm": 250.0,
            "_circle": {"center": [0.0, 0.0], "radius": 62.5},
        }
    ]
    _diameters_from_circles(drawing, requests, plan)

    values = [item["value_mm"] for item in drawing["dimensions"]]
    assert values == [250.0]  # ноль TechDraw убран, диаметр — из окружности


def test_concentric_diameters_do_not_share_one_line():
    """Ø250 и Ø66 под одним 45° легли подписями в центр: «Ø2560»."""
    from app.ai.cad_ir.sheet_from_solid import _diameters_from_circles

    plan = plan_sheet(_FLANGE, _FLANGE_REPORT)
    plan.ratio = 1.0
    drawing = {"dimensions": []}
    requests = [
        {
            "view_index": 0,
            "kind": "Diameter",
            "_nominal_mm": d,
            "_circle": {"center": [0.0, 0.0], "radius": d / 2},
        }
        for d in (250.0, 66.0)
    ]
    _diameters_from_circles(drawing, requests, plan)

    directions = {
        (
            round(item["anchors_mm"][1][0] / (item["value_mm"] / 2), 2),
            round(item["anchors_mm"][1][1] / (item["value_mm"] / 2), 2),
        )
        for item in drawing["dimensions"]
    }
    assert len(directions) == 2


def test_a_chain_leaves_exactly_one_link_open_even_with_repeated_lengths():
    """Вал shaft-1 корпуса: ступени 12/30/35/80/15/80/15.

    Множество значений без всех «самых длинных» дало 12, 30, 35 и одну 15 —
    открытыми остались три звена вместо одного.
    """
    from app.ai.cad_ir.sheet_from_solid import _chain_lengths

    outer = [{"d": 25, "l": value} for value in (12, 30, 35, 80, 15, 80, 15)]

    assert _chain_lengths(outer) == [12.0, 15.0, 15.0, 30.0, 35.0, 80.0]
    assert _chain_lengths([{"d": 20, "l": 50}]) == []


# ── Плоская деталь: главный вид — план (базовая линия M5, plate-0) ─────────
# Ядро для пластины 100×50×25: front — u 25 (толщина), v 100 (ширина);
# side — u 100, v 50 (план); top — u 25, v 50. Лист вёл вид ребром первым —
# вертикальную полоску 25×100, — и модель чтения прочитала ширину 25.

_PLATE = {
    "main_view": {
        "profile": {"shape": "rectangle", "width_mm": 100, "height_mm": 50, "thickness_mm": 25}
    },
    "views": [],
}


def test_a_plate_sheet_is_its_plan_with_the_thickness_view_beside_it():
    kinds = [view["kind"] for view in plan_views("plate", _PLATE)]

    assert kinds == ["front", "side", "top"]  # front — только основа ядра


def test_a_flange_keeps_its_axial_section_beside_the_plan():
    kinds = [view["kind"] for view in plan_views("flange", _PLATE)]

    assert kinds == ["front", "section", "side"]


def test_the_thickness_view_of_a_plate_stands_right_of_the_plan():
    from app.ai.cad_projection import place_sheet_views

    views = [
        {"kind": "front", "bounds_mm": {"u_min": 0, "u_max": 25, "v_min": 0, "v_max": 100}},
        {"kind": "side", "bounds_mm": {"u_min": 0, "u_max": 100, "v_min": 0, "v_max": 50}},
        {"kind": "top", "bounds_mm": {"u_min": 0, "u_max": 25, "v_min": 0, "v_max": 50}},
    ]

    _entities, placements = place_sheet_views(views, px_per_mm=1.0, skip={0}, right={2})

    assert placements[0] is None  # основа на лист не идёт
    assert placements[1]["offset_u"] == 0.0  # план — главный вид
    assert placements[2]["offset_u"] > 100.0  # толщина — справа от плана
    assert placements[2]["offset_v"] == placements[1]["offset_v"]  # на одной оси


# ── X1: где стоят отверстия плоской детали ──────────────────────────────────
# Числа — из пробы ядра: план пластины 100×50 (u, v от центра), отверстия
# (10, 9) Ø13.5 и (−28, 14) Ø11; фланец Ø120, центр Ø32, 4 отв. Ø5.5 на Ø73.


def _plan(part_class: str):
    from types import SimpleNamespace

    return SimpleNamespace(part_class=part_class, ratio=1.0, scaffold_views={0})


def _circle(u: float, v: float, r: float) -> dict:
    return {"type": "circle", "center": [u, v], "radius": r}


def test_plate_holes_get_coordinates_from_the_left_and_bottom_edges():
    from app.ai.cad_ir.sheet_from_solid import _hole_dimensions

    drawing = {
        "views": [
            {"kind": "front"},
            {
                "kind": "side",
                "bounds_mm": {"u_min": -50, "u_max": 50, "v_min": -25, "v_max": 25},
                "visible": [_circle(10, 9, 6.75), _circle(-28, 14, 5.5)],
            },
        ],
        "dimensions": [
            {
                "view_index": 1,
                "kind": "DistanceY",
                "value_mm": 50.0,
                "anchors_mm": [[-50, -25], [-50, 25]],
            }
        ],
    }

    _hole_dimensions(drawing, _plan("plate"))

    xs = sorted(d["value_mm"] for d in drawing["dimensions"] if d["kind"] == "DistanceX")
    ys = sorted(
        d["value_mm"]
        for d in drawing["dimensions"]
        if d["kind"] == "DistanceY" and d.get("measured_by") == "hole_centre"
    )
    assert xs == [22.0, 60.0]
    assert ys == [34.0, 39.0]
    height = drawing["dimensions"][0]
    assert height["outside"] and height["place_u"] == -50  # высота — в тех же рядах слева


def test_a_bolt_circle_gets_its_pitch_diameter_and_the_hole_count():
    from app.ai.cad_ir.sheet_from_solid import _hole_dimensions

    bolts = [
        _circle(36.5, 0, 2.75),
        _circle(0, 36.5, 2.75),
        _circle(-36.5, 0, 2.75),
        _circle(0, -36.5, 2.75),
    ]
    drawing = {
        "views": [
            {"kind": "front"},
            {"kind": "section"},
            {
                "kind": "side",
                "bounds_mm": {"u_min": -60, "u_max": 60, "v_min": -60, "v_max": 60},
                "visible": [_circle(0, 0, 60), _circle(0, 0, 16), *bolts],
            },
        ],
        "dimensions": [
            {"view_index": 2, "kind": "Diameter", "value_mm": 5.5, "label": "Ø5.5"},
        ],
    }

    _hole_dimensions(drawing, _plan("flange"))

    pitch = [d for d in drawing["dimensions"] if d.get("pitch_circle")]
    assert [d["value_mm"] for d in pitch] == [73.0]
    assert drawing["dimensions"][0]["label"] == "4 отв. Ø5.5"
    # Отверстия на окружности центров координат не получают.
    assert not [d for d in drawing["dimensions"] if d.get("measured_by") == "hole_centre"]


def test_a_rotation_body_gets_no_hole_coordinates():
    from app.ai.cad_ir.sheet_from_solid import _hole_dimensions

    drawing = {
        "views": [
            {
                "kind": "side",
                "bounds_mm": {"u_min": -10, "u_max": 10, "v_min": -10, "v_max": 10},
                "visible": [_circle(5, 5, 1)],
            }
        ]
    }
    _hole_dimensions(drawing, _plan("solid_rotation"))
    assert not drawing.get("dimensions")


def _plate_with_corners(width: float, height: float, sx: float, sy: float) -> dict:
    corners = [_circle(x, y, 3.3) for x in (-sx / 2, sx / 2) for y in (-sy / 2, sy / 2)]
    return {
        "views": [
            {"kind": "front"},
            {
                "kind": "side",
                "bounds_mm": {
                    "u_min": -width / 2,
                    "u_max": width / 2,
                    "v_min": -height / 2,
                    "v_max": height / 2,
                },
                "visible": corners,
            },
        ],
        "dimensions": [],
    }


def test_corner_holes_of_a_plate_get_coordinates_not_a_bolt_circle():
    """Корпус v4, plate-3: через 4 угловых отверстия лист провёл окружность Ø97.529."""
    from app.ai.cad_ir.sheet_from_solid import _hole_dimensions

    for width, height, sx, sy in ((60, 100, 46, 86), (80, 80, 60, 60)):  # и квадрат
        drawing = _plate_with_corners(width, height, sx, sy)
        _hole_dimensions(drawing, _plan("plate"))

        assert not [d for d in drawing["dimensions"] if d.get("pitch_circle")]
        xs = sorted(d["value_mm"] for d in drawing["dimensions"] if d["kind"] == "DistanceX")
        assert xs == [(width - sx) / 2, (width + sx) / 2]


def test_unevenly_spaced_holes_of_a_flange_are_not_a_bolt_circle():
    from app.ai.cad_ir.sheet_from_solid import _is_bolt_circle

    even = [(30.0, 0.0, 3.0), (0.0, 30.0, 3.0), (-30.0, 0.0, 3.0), (0.0, -30.0, 3.0)]
    uneven = [(30.0, 0.0, 3.0), (0.0, 30.0, 3.0), (-30.0, 0.0, 3.0), (21.2, -21.2, 3.0)]

    assert _is_bolt_circle(even, 0.0, 0.0, "flange")
    assert not _is_bolt_circle(uneven, 0.0, 0.0, "flange")
    assert not _is_bolt_circle(even, 0.0, 0.0, "plate")


def _arc(u: float, v: float, r: float, start: tuple, end: tuple) -> dict:
    return {"type": "arc", "center": [u, v], "radius": r, "points": [list(start), list(end)]}


def test_plate_corner_fillets_get_one_radius_and_slot_ends_do_not():
    """Базовая линия M5 v2: «радиус скругления углов не указан на чертеже» — 0/2."""
    from app.ai.cad_ir.sheet_from_solid import _hole_dimensions

    corners = [
        _arc(-45, 20, 5, (-50, 20), (-45, 25)),
        _arc(45, 20, 5, (45, 25), (50, 20)),
        _arc(45, -20, 5, (50, -20), (45, -25)),
        _arc(-45, -20, 5, (-45, -25), (-50, -20)),
    ]
    slot_ends = [_arc(-10, 0, 4, (-10, 4), (-10, -4)), _arc(10, 0, 4, (10, -4), (10, 4))]
    drawing = {
        "views": [
            {"kind": "front"},
            {
                "kind": "side",
                "bounds_mm": {"u_min": -50, "u_max": 50, "v_min": -25, "v_max": 25},
                "visible": corners + slot_ends,
            },
        ],
        "dimensions": [],
    }

    _hole_dimensions(drawing, _plan("plate"))

    # Концы прорези — свой размер «R» (см. тест прорези), но не скругление угла.
    radii = [d for d in drawing["dimensions"] if d.get("measured_by") == "view_arc"]
    assert [(d["value_mm"], d["label"]) for d in radii] == [(5.0, "R5")]
    (cu, cv), (tu, tv) = radii[0]["anchors_mm"]
    assert abs(tu) > abs(cu) and abs(tv) > abs(cv)  # к углу, наружу
    # правый нижний угол: левый нижний занят выносными координат отверстий
    assert cu > 0 and cv < 0


def test_a_slot_gets_its_centre_its_centre_distance_and_its_end_radius():
    """Корпус v4, plate-3: прорезь 25×6 с центром (3, −15) стояла без размеров.

    Геометрия — как её отдаёт ядро: левый конец полуокружностью, правый двумя
    четвертями с общим центром.
    """
    from app.ai.cad_ir.sheet_from_solid import _hole_dimensions

    drawing = {
        "views": [
            {"kind": "front"},
            {
                "kind": "side",
                "bounds_mm": {"u_min": -30, "u_max": 30, "v_min": -50, "v_max": 50},
                "visible": [
                    _arc(-6.5, -15, 3, (-6.5, -12), (-6.5, -18)),
                    _arc(12.5, -15, 3, (12.5, -18), (15.5, -15)),
                    _arc(12.5, -15, 3, (15.5, -15), (12.5, -12)),
                ],
            },
        ],
        "dimensions": [],
    }

    _hole_dimensions(drawing, _plan("plate"))

    slot = {(d["kind"], d["value_mm"]) for d in drawing["dimensions"] if d["measured_by"] == "slot"}
    # Межцентровое — под видом: над ним выносная координаты центра шла через середину.
    assert all(
        d.get("below")
        for d in drawing["dimensions"]
        if d["measured_by"] == "slot" and d["kind"] == "DistanceX"
    )
    centre = {
        (d["kind"], d["value_mm"])
        for d in drawing["dimensions"]
        if d["measured_by"] == "hole_centre"
    }
    assert slot == {("DistanceX", 19.0), ("Radius", 3.0)}
    assert centre == {("DistanceX", 33.0), ("DistanceY", 35.0)}
    assert not any("отв." in str(d.get("label")) for d in drawing["dimensions"])


# ── Ф3.0a: главный вид вала с пазом — лицом к пазу ─────────────────────────
# Проба ядра на shaft-1: с `front` концы паза — дуги у верхней кромки
# (полоска), с `top` — невидимые линии (паз на обратной стороне, угол 0 = −X).


def test_a_keyed_shaft_is_drawn_facing_its_keyway():
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 30, "length_mm": 50}],
            "keyways": [{"axial_start_mm": 10, "length_mm": 20, "width_mm": 8, "depth_mm": 4}],
        },
        "views": [],
    }
    kinds = [view["kind"] for view in plan_views("solid_rotation", spec)]

    assert kinds[:2] == ["front", "bottom"]
    assert "side" in kinds


def test_a_plain_shaft_keeps_its_front_view():
    spec = {"main_view": {"outer": [{"diameter_mm": 30, "length_mm": 50}]}, "views": []}
    assert "bottom" not in [view["kind"] for view in plan_views("solid_rotation", spec)]


def test_the_front_view_of_a_keyed_shaft_is_only_a_scaffold():
    from app.ai.cad_ir.sheet_from_solid import _view_reasons

    views = [{"kind": "front"}, {"kind": "bottom"}, {"kind": "side"}]
    reasons = {
        item["kind"]: item for item in _view_reasons(views, "solid_rotation", {"main_view": {}})
    }

    assert reasons["front"]["visible"] is False
    assert "паз" in reasons["bottom"]["reason"]


# ── Ф3.0b: паз и поперечные отверстия на главном виде вала ─────────────────
# Числа — из пробы ядра, shaft-1, вид `bottom` (u от −133,5 до 133,5, 1:1).


def _arc_mid(u, v, r, start, end, mid):
    return {
        "type": "arc",
        "center": [u, v],
        "radius": r,
        "points": [list(start), list(end)],
        "mid": list(mid),
    }


def _shaft_bottom_view() -> dict:
    return {
        "kind": "bottom",
        "bounds_mm": {"u_min": -133.5, "u_max": 133.5, "v_min": -20, "v_max": 20},
        "visible": [
            # паз 1 (16,3..35,3, R4): концы — по две четверти
            _arc_mid(-113.2, 0, 4, (-117.2, 0), (-113.2, 4), (-116.03, 2.83)),
            _arc_mid(-113.2, 0, 4, (-113.2, -4), (-117.2, 0), (-116.03, -2.83)),
            _arc_mid(-102.2, 0, 4, (-102.2, 4), (-98.2, 0), (-99.37, 2.83)),
            _arc_mid(-102.2, 0, 4, (-98.2, 0), (-102.2, -4), (-99.37, -2.83)),
            # паз 2 (188,7..241,7, R5): левый конец — полуокружность, правый — две четверти
            _arc_mid(60.2, 0, 5, (60.2, -5), (60.2, 5), (55.2, 0)),
            _arc_mid(103.2, 0, 5, (103.2, 5), (108.2, 0), (106.74, 3.54)),
            _arc_mid(103.2, 0, 5, (108.2, 0), (103.2, -5), (106.74, -3.54)),
            # поперечное Ø5 на 166,8 — две полуокружности
            _arc_mid(33.3, 0, 2.5, (30.8, 0), (35.8, 0), (33.3, 2.5)),
            _arc_mid(33.3, 0, 2.5, (35.8, 0), (30.8, 0), (33.3, -2.5)),
            # поперечное Ø4 на 64,7 — 150,5° + 29,5° + 180°
            _arc_mid(-68.8, 0, 2, (-66.8, 0), (-70.54, -0.98), (-68.29, -1.93)),
            _arc_mid(-68.8, 0, 2, (-70.54, -0.98), (-70.8, 0), (-70.73, -0.51)),
            _arc_mid(-68.8, 0, 2, (-70.8, 0), (-66.8, 0), (-68.8, 2)),
        ],
    }


def test_arcs_merge_into_keyway_ends_and_cross_holes_by_their_total_sweep():
    from app.ai.cad_ir.sheet_from_solid import _arc_groups, _capsules

    view = _shaft_bottom_view()
    groups = _arc_groups(view, view["bounds_mm"])

    holes = sorted(round(r * 2, 1) for _u, _v, r, sweep in groups if abs(sweep - 360) <= 30)
    assert holes == [4.0, 5.0]
    capsules = _capsules(groups)
    assert sorted((round(low[0], 1), round(high[0], 1)) for low, high, _r in capsules) == [
        (-113.2, -102.2),
        (60.2, 103.2),
    ]


def test_keyways_and_cross_holes_are_dimensioned_from_their_shoulder():
    """Базовая линия v2: пазы читались наугад, поперечные отверстия — никак."""
    from types import SimpleNamespace

    from app.ai.cad_ir.sheet_from_solid import _shaft_feature_dimensions

    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": diameter, "length_mm": length}
                for diameter, length in (
                    (25, 12),
                    (28, 30),
                    (25, 35),
                    (40, 80),
                    (30, 15),
                    (35, 80),
                    (22, 15),
                )
            ]
        }
    }
    drawing = {"views": [{"kind": "front"}, _shaft_bottom_view()], "dimensions": []}
    plan = SimpleNamespace(part_class="solid_rotation", ratio=1.0, scaffold_views={0})

    _shaft_feature_dimensions(drawing, spec, plan)

    keyway = sorted(d["value_mm"] for d in drawing["dimensions"] if d["measured_by"] == "keyway")
    holes = sorted(
        (d["kind"], d["value_mm"])
        for d in drawing["dimensions"]
        if d["measured_by"] == "cross_hole"
    )
    assert keyway == [4.3, 16.7, 19.0, 53.0]  # положения от уступов 12 и 172, длины
    assert holes == [("Diameter", 4.0), ("Diameter", 5.0), ("DistanceX", 9.8), ("DistanceX", 22.7)]
    assert all(d.get("below") for d in drawing["dimensions"] if d["kind"] == "DistanceX")


def test_a_plate_slot_is_unchanged_by_the_shared_arc_grouping():
    """`_slots` пластины перешёл на общий разбор дуг — прорезь та же."""
    from app.ai.cad_ir.sheet_from_solid import _hole_dimensions

    drawing = {
        "views": [
            {"kind": "front"},
            {
                "kind": "side",
                "bounds_mm": {"u_min": -30, "u_max": 30, "v_min": -50, "v_max": 50},
                "visible": [
                    _arc_mid(-6.5, -15, 3, (-6.5, -12), (-6.5, -18), (-9.5, -15)),
                    _arc_mid(12.5, -15, 3, (12.5, -18), (15.5, -15), (14.62, -17.12)),
                    _arc_mid(12.5, -15, 3, (15.5, -15), (12.5, -12), (14.62, -12.88)),
                ],
            },
        ],
        "dimensions": [],
    }
    _hole_dimensions(drawing, _plan("plate"))
    slot = {(d["kind"], d["value_mm"]) for d in drawing["dimensions"] if d["measured_by"] == "slot"}
    assert slot == {("DistanceX", 19.0), ("Radius", 3.0)}


def test_a_hollow_keyed_shaft_gets_a_view_facing_its_keyway_below_the_section():
    """shaft-2/-7/-10 корпуса: у полого вала элементы оставались без размеров."""
    spec = {
        "main_view": {
            "outer": [{"diameter_mm": 40, "length_mm": 60}],
            "bore": [{"diameter_mm": 20, "length_mm": 60}],
            "keyways": [{"axial_start_mm": 10, "length_mm": 20, "width_mm": 12, "depth_mm": 5}],
        },
        "views": [],
    }
    kinds = [view["kind"] for view in plan_views("hollow_rotation", spec)]

    assert "section" in kinds and "bottom" in kinds


def test_the_keyway_view_of_a_hollow_shaft_stands_below_its_section():
    from app.ai.cad_projection import place_sheet_views

    views = [
        {"kind": "front", "bounds_mm": {"u_min": -30, "u_max": 30, "v_min": -20, "v_max": 20}},
        {"kind": "section", "bounds_mm": {"u_min": -30, "u_max": 30, "v_min": -20, "v_max": 20}},
        {"kind": "bottom", "bounds_mm": {"u_min": -30, "u_max": 30, "v_min": -20, "v_max": 20}},
    ]
    _entities, placements = place_sheet_views(views, px_per_mm=1.0, skip={0}, anchor=1)

    assert placements[1]["offset_u"] == 30.0  # главный вид полого вала — разрез, в начале листа
    assert placements[2]["offset_u"] == placements[1]["offset_u"]  # общая ось u
    assert placements[2]["offset_v"] > placements[1]["offset_v"] + 40  # ниже разреза


def test_feature_dimensions_of_a_hollow_shaft_are_placed_too():
    from types import SimpleNamespace

    from app.ai.cad_ir.sheet_from_solid import _shaft_feature_dimensions

    spec = {
        "main_view": {
            "outer": [
                {"diameter_mm": diameter, "length_mm": length}
                for diameter, length in (
                    (25, 12),
                    (28, 30),
                    (25, 35),
                    (40, 80),
                    (30, 15),
                    (35, 80),
                    (22, 15),
                )
            ]
        }
    }
    drawing = {
        "views": [{"kind": "front"}, {"kind": "section"}, _shaft_bottom_view()],
        "dimensions": [],
    }
    plan = SimpleNamespace(part_class="hollow_rotation", ratio=1.0, scaffold_views={0})

    _shaft_feature_dimensions(drawing, spec, plan)

    assert any(d["measured_by"] == "keyway" for d in drawing["dimensions"])


def test_a_hollow_shaft_is_laid_out_around_its_section():
    """Правило «первый вид, что не разрез» делало опорным вид с торца."""
    from app.ai.cad_ir.sheet_from_solid import plan_sheet

    spec = {
        **_HOLLOW,
        "main_view": {
            **_HOLLOW["main_view"],
            "keyways": [{"axial_start_mm": 10, "length_mm": 20, "width_mm": 12, "depth_mm": 5}],
        },
    }
    plan = plan_sheet(spec, _SHAFT_REPORT)

    assert plan.part_class == "hollow_rotation"
    assert plan.views[plan.anchor_view]["kind"] == "section"
    assert "bottom" in [view["kind"] for view in plan.views]


def test_arcs_the_kernel_returns_twice_are_counted_once():
    """shaft-18: отверстие Ø4 пришло дугами 180+150,5+29,5+180 — сумма 540°."""
    from app.ai.cad_ir.sheet_from_solid import _arc_groups

    hole = [
        _arc_mid(0, 0, 2, (2, 0), (-2, 0), (0, 2)),
        _arc_mid(0, 0, 2, (2, 0), (-1.74, -0.98), (0.51, -1.93)),
        _arc_mid(0, 0, 2, (-1.74, -0.98), (-2, 0), (-1.93, -0.51)),
        _arc_mid(0, 0, 2, (-2, 0), (2, 0), (0, 2)),  # та же верхняя половина второй раз
    ]
    view = {"visible": hole}
    groups = _arc_groups(view, {"u_min": -50, "u_max": 50, "v_min": -20, "v_max": 20})

    assert len(groups) == 1 and abs(groups[0][3] - 360.0) < 1.0


def test_outer_and_bore_diameters_of_one_step_do_not_share_a_place():
    """shaft-2 корпуса: «Ø35» полого вала лёг поверх «Ø15» расточки."""
    from app.ai.cad_ir.sheet_from_solid import _diameter_requests

    view = {
        "visible": [
            {"type": "line", "edge_index": 1, "points": [[0.0, 17.5], [60.0, 17.5]]},
            {"type": "line", "edge_index": 2, "points": [[0.0, -17.5], [60.0, -17.5]]},
            {"type": "line", "edge_index": 3, "points": [[0.0, 7.5], [60.0, 7.5]]},
            {"type": "line", "edge_index": 4, "points": [[0.0, -7.5], [60.0, -7.5]]},
        ]
    }
    requests = _diameter_requests(view, 0, [35.0, 15.0], 1.0)

    places = sorted(r["_place_u"] for r in requests)
    assert len(places) == 2
    assert places[1] - places[0] >= 2.5 * 3.5
    assert all(0.0 <= place <= 60.0 for place in places)


_KEYED = {
    "part": "Вал",
    "main_view": {
        "type": "тело вращения (вал)",
        "outer": [
            {"diameter_mm": 30.0, "length_mm": 40.0},
            {"diameter_mm": 22.0, "length_mm": 60.0},
        ],
        "keyways": [
            {"axial_start_mm": 10.0, "length_mm": 20.0, "width_mm": 8.0, "depth_mm": 4.0},
            {
                "axial_start_mm": 60.0,
                "length_mm": 25.0,
                "width_mm": 6.0,
                "depth_mm": 3.5,
                "angle_deg": 90.0,
            },
        ],
    },
    "views": [{"kind": "removed_section", "label": "А-А"}],
}


def test_a_keyed_shaft_gets_a_removed_cross_section_through_each_keyway():
    """X1b: b и t1 паза ставятся на вынесенном сечении — поперёк оси, на середине
    паза. Прочитанное вынесенное сечение резалось вдоль вида и выреза не имело."""
    views = plan_views("solid_rotation", _KEYED)

    sections = [v for v in views if v.get("section_normal") == "axis"]
    assert [(v["section_station_mm"], v["label"]) for v in sections] == [
        (16.667, "Б-Б"),
        (68.333, "В-В"),
    ]
    assert all(v["presentation_kind"] == "removed_section" for v in sections)
    assert not [v for v in views if v.get("label") == "А-А"], "продольное сечение из чтения"
    assert len(views) <= 8


def test_every_keyway_of_a_four_keyway_shaft_gets_its_own_section():
    """Корпус X1b: у вала с четырьмя пазами (shaft-4) четвёртому сечению не
    хватало места в запросе к ядру (6 видов) — b и t1 паза не проставлялись."""
    spec = {
        "part": "Вал",
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30.0, "length_mm": 50.0},
                {"diameter_mm": 25.0, "length_mm": 50.0},
                {"diameter_mm": 22.0, "length_mm": 50.0},
                {"diameter_mm": 18.0, "length_mm": 50.0},
            ],
            "keyways": [
                {
                    "axial_start_mm": 10.0 + 50.0 * i,
                    "length_mm": 20.0,
                    "width_mm": 6.0,
                    "depth_mm": 3.5,
                }
                for i in range(4)
            ],
        },
    }
    views = plan_views("solid_rotation", spec)

    sections = [v for v in views if v.get("section_normal") == "axis"]
    assert len(sections) == 4
    assert len(views) <= 8


def test_keyway_width_and_depth_are_dimensioned_on_its_cross_section():
    from app.ai.cad_ir.sheet_from_solid import _keyway_section_dimensions

    drawing = {
        "views": [
            {"kind": "front"},
            {
                "kind": "removed_section",
                "section_station_mm": 20.0,
                "bounds_mm": {"u_min": -15, "u_max": 11, "v_min": -15, "v_max": 15},
            },
            {
                "kind": "removed_section",
                "section_station_mm": 72.5,
                "bounds_mm": {"u_min": -11, "u_max": 11, "v_min": -11, "v_max": 7.5},
            },
        ]
    }

    _keyway_section_dimensions(drawing, _KEYED, _plan("solid_rotation"))

    by_view = {}
    for dim in drawing["dimensions"]:
        by_view.setdefault(dim["view_index"], []).append(dim)
    first = {d["measured_by"]: d for d in by_view[1]}
    assert first["keyway_section_width"]["value_mm"] == 8.0
    assert first["keyway_section_width"]["kind"] == "DistanceY"
    (a, b) = first["keyway_section_depth"]["anchors_mm"]
    assert abs(a[0] - 11.0) < 1e-6 and abs(b[0] - 15.0) < 1e-6  # от дна до поверхности Ø30
    second = {d["measured_by"]: d for d in by_view[2]}
    # Паз под 90° — вдоль v: ширина по u, глубина по v, Ø22.
    assert second["keyway_section_width"]["kind"] == "DistanceX"
    (a, b) = second["keyway_section_depth"]["anchors_mm"]
    assert abs(a[1] - 7.5) < 1e-6 and abs(b[1] - 11.0) < 1e-6


def test_section_views_carry_their_designation_above_them():
    """Надписей «Б-Б» над вынесенными сечениями и «А-А» над разрезом лист не
    выводил — связать сечение с листом было нечем."""
    from app.ai.cad_ir.sheet_from_solid import _view_label_entities

    views = [
        {"kind": "front", "bounds_mm": {"u_min": 0, "u_max": 10, "v_min": -5, "v_max": 5}},
        {
            "kind": "removed_section",
            "label": "Б-Б",
            "bounds_mm": {"u_min": -15, "u_max": 15, "v_min": -15, "v_max": 15},
        },
        {
            "kind": "side",
            "label": "ignored",
            "bounds_mm": {"u_min": -15, "u_max": 15, "v_min": -15, "v_max": 15},
        },
    ]
    placements = [{"offset_u": 0, "offset_v": 50}, {"offset_u": 100, "offset_v": 50}, None]

    (label,) = _view_label_entities(views, placements)

    assert label.text == "Б-Б"
    assert label.position.y < (50 - 15) * 4.0  # над контуром сечения


def test_removed_section_has_its_cutting_plane_marked_on_the_main_view():
    """ГОСТ 2.305: у вынесенного сечения «Б-Б» на главном виде — разомкнутая
    линия на станции сечения, стрелки и буква. Без неё сечение не связать с
    местом на валу."""
    from app.ai.cad_ir.sheet_from_solid import PAPER_PX_PER_MM, _cutting_plane_entities

    views = [
        {"kind": "front", "bounds_mm": {"u_min": -50, "u_max": 50, "v_min": -15, "v_max": 15}},
        {"kind": "bottom", "bounds_mm": {"u_min": -50, "u_max": 50, "v_min": -15, "v_max": 15}},
        {
            # Как отдаёт ядро: removed_section со станцией, без section_normal.
            "kind": "removed_section",
            "section_station_mm": 20.0,
            "label": "Б-Б",
            "bounds_mm": {"u_min": -15, "u_max": 15, "v_min": -15, "v_max": 15},
        },
    ]
    placements = [
        None,
        {"offset_u": 100.0, "offset_v": 80.0},
        {"offset_u": 250.0, "offset_v": 80.0},
    ]
    plan = _plan("solid_rotation")
    plan.ratio = 0.5

    entities = _cutting_plane_entities(views, placements, plan)

    strokes = [e for e in entities if e.type == "segment" and e.line_class == "contour"]
    assert len(strokes) == 2
    station_u = (100.0 - 50.0 + 20.0 * 0.5) * PAPER_PX_PER_MM
    assert all(abs(e.p1.x - station_u) < 1e-6 and abs(e.p2.x - station_u) < 1e-6 for e in strokes)
    # Штрихи за контуром: выше v_max и ниже v_min вида, не через деталь.
    ys = sorted(y / PAPER_PX_PER_MM for e in strokes for y in (e.p1.y, e.p2.y))
    assert ys[1] < 80.0 - 15.0 and ys[2] > 80.0 + 15.0
    letters = [e.text for e in entities if e.type == "text"]
    assert letters == ["Б", "Б"]


def test_no_cutting_plane_without_an_axial_section():
    from app.ai.cad_ir.sheet_from_solid import _cutting_plane_entities

    views = [{"kind": "bottom", "bounds_mm": {"u_min": 0, "u_max": 10, "v_min": 0, "v_max": 5}}]
    assert (
        _cutting_plane_entities(
            views, [{"offset_u": 0.0, "offset_v": 0.0}], _plan("solid_rotation")
        )
        == []
    )


_HOUSING = {
    "part": "Корпус",
    "main_view": {
        "type": "корпус",
        "profile": {
            "shape": "rectangle",
            "width_mm": 100.0,
            "height_mm": 80.0,
            "thickness_mm": 40.0,
            "wall_features": [
                {
                    "kind": "pocket",
                    "on_plane": "top",
                    "profile": "rectangle",
                    "width_mm": 70.0,
                    "height_mm": 50.0,
                    "depth_mm": 32.0,
                },
                {
                    "kind": "boss",
                    "on_plane": "front",
                    "profile": "circle",
                    "diameter_mm": 25.0,
                    "depth_mm": 8.0,
                    "center_u_mm": 10.0,
                    "center_v_mm": 0.0,
                },
            ],
        },
    },
}


def test_a_housing_shows_its_front_view_with_the_width_horizontal():
    """У призматической детали ядро кладёт ширину вертикально: вид спереди на
    лист выводить было нельзя, а элементы передних стенок видны только на нём."""
    views = plan_views("plate", _HOUSING)

    front = next(view for view in views if view["kind"] == "front")
    assert front["x_direction"] == [1.0, 0.0, 0.0]
    # У пластины без элементов стенок вид спереди остаётся служебным.
    plain = {"main_view": {"profile": {"shape": "rectangle", "width_mm": 50, "height_mm": 40}}}
    assert "x_direction" not in next(v for v in plan_views("plate", plain) if v["kind"] == "front")


def test_a_housing_dimensions_its_wall_features_from_the_drawn_geometry():
    from app.ai.cad_ir.sheet_from_solid import _wall_feature_dimensions

    ratio = 1.0
    plan = _plan("plate")
    plan.ratio = ratio
    plan.scaffold_views = set()

    def rect(u0, u1, v0, v1):
        return [
            {"type": "line", "points": [[u0, v0], [u1, v0]]},
            {"type": "line", "points": [[u0, v1], [u1, v1]]},
            {"type": "line", "points": [[u0, v0], [u0, v1]]},
            {"type": "line", "points": [[u1, v0], [u1, v1]]},
        ]

    # Вид смещён вылетом прилива: кромки тела не по центру вида (как у корпуса
    # G2 — план −23,5…+26,5 при габарите 100).
    drawing = {
        "views": [
            {
                # Вид спереди: ширина по u, толщина по v; прилив Ø25 нарисован.
                "kind": "front",
                "x_direction": [1.0, 0.0, 0.0],
                "bounds_mm": {"u_min": -50, "u_max": 50, "v_min": -20, "v_max": 28},
                "visible": [
                    *rect(-50.0, 50.0, -20.0, 20.0),
                    {"type": "circle", "center": [-40.0, 0.0], "radius": 12.5},
                ],
                "hidden": [{"type": "line", "points": [[-35.0, -12.0], [35.0, -12.0]]}],
            },
            {
                # План: ширина × высота, полость прямоугольником, прилив за кромкой.
                "kind": "side",
                "bounds_mm": {"u_min": -50, "u_max": 50, "v_min": -48, "v_max": 40},
                "visible": [
                    *rect(-50.0, 50.0, -40.0, 40.0),
                    *rect(-35.0, 35.0, -25.0, 25.0),
                    {"type": "line", "points": [[-52.5, -48.0], [-27.5, -48.0]]},
                ],
                "hidden": [],
            },
            {"kind": "top", "bounds_mm": {"u_min": -20, "u_max": 20, "v_min": -40, "v_max": 40}},
        ]
    }

    _wall_feature_dimensions(drawing, _HOUSING, plan)

    by_measure = {}
    for dim in drawing["dimensions"]:
        by_measure.setdefault(dim["measured_by"], []).append(round(dim["value_mm"], 2))
    assert 25.0 in by_measure["wall_feature"]  # Ø прилива
    assert sorted(by_measure["wall_feature"]) == [25.0, 50.0, 70.0]  # полость 70 × 50
    assert 8.0 in by_measure["wall_feature_depth"]  # вылет прилива за кромку
    # Габарит — по кромкам ТЕЛА: за крайние линии вида выходят приливы.
    assert sorted(by_measure["housing_overall"]) == [40.0, 80.0, 100.0]
    # Координаты — от кромок тела, а не от края вида: прилив на 10 от середины.
    assert sorted(by_measure["wall_feature_position"]) == [10.0, 20.0, 40.0, 50.0]


# ── Листовая деталь (X4) ─────────────────────────────────────────────────────


def _channel() -> dict:
    return {
        "main_view": {
            "type": "листовая деталь",
            "sheet_metal": {
                "flanges_mm": [15.0, 40.0, 12.0],
                "turns": [1, 1],
                "radius_mm": 2.0,
                "thickness_mm": 2.0,
                "width_mm": 50.0,
            },
        }
    }


def _drawn_section(sketch: list[dict], ratio: float, flip_u: bool) -> dict:
    """Вид ядра: эскиз, отражённый по u и сдвинутый, — как ядро кладёт оси по-своему."""
    points = [(0.0, 0.0)] + [tuple(segment["to"]) for segment in sketch]
    sign = -1.0 if flip_u else 1.0
    placed = [(sign * x * ratio + 7.0, y * ratio - 3.0) for x, y in points]
    return {
        "visible": [
            {"type": "line", "points": [list(a), list(b)]}
            for a, b in zip(placed, placed[1:], strict=False)
        ]
    }


def test_a_sheet_metal_part_is_drawn_as_its_section_with_the_width_beside():
    """Раньше гнутая деталь шла классом «фланец» (по габариту): разрез полоской
    и ни одного размера — ни полок, ни толщины, ни развёртки."""
    from app.ai.cad_ir.sheet_from_solid import classify_part, plan_views

    spec = _channel()
    assert classify_part(spec, {"bounds_mm": {"x": 20, "y": 44, "z": 50}}) == "sheet_metal"
    assert [view["kind"] for view in plan_views("sheet_metal", spec)] == ["front", "side", "top"]


def test_flange_dimensions_land_on_the_drawn_section_whatever_its_axes():
    """Эскиз сечения и вид ядра — в разных осях: размер, положенный по числам
    эскиза без перевода, встал бы мимо детали."""
    from app.ai.cad_ir.sheet_from_solid import SheetPlan, _sheet_metal_dimensions
    from app.ai.sheet_metal import bent_section

    spec = _channel()
    sheet = spec["main_view"]["sheet_metal"]
    sketch = bent_section(sheet["flanges_mm"], sheet["turns"], 2.0, 2.0)
    plan = SheetPlan(
        part_class="sheet_metal",
        views=[{"kind": "front"}, {"kind": "side"}, {"kind": "top"}],
        sheet_format="A4",
        landscape=True,
        ratio=0.5,
        scale_label="1:2",
        layout_w_mm=0.0,
        layout_h_mm=0.0,
    )
    drawing = {
        "views": [
            {},
            _drawn_section(sketch, 0.5, flip_u=True),
            {"bounds_mm": {"u_min": 0.0, "u_max": 25.0, "v_min": 0.0, "v_max": 22.0}},
        ],
        "dimensions": [],
    }

    _sheet_metal_dimensions(drawing, spec, plan)

    labels = sorted(item["label"] for item in drawing["dimensions"])
    # Полки по наружной поверхности: прямой участок + (R + s) на каждый гиб.
    assert labels == sorted(["19", "48", "16", "s2", "50"])
    web = next(item for item in drawing["dimensions"] if item["label"] == "48")
    (u1, v1), (u2, v2) = web["anchors_mm"]
    assert web["kind"] == "DistanceY"
    assert abs(abs(v2 - v1) - 48 * 0.5) < 1e-6
    # Наружная поверхность стенки в эскизе — x = 15 + R + s = 19; вид отражён
    # по u и сдвинут на 7: u = 7 − 19·0,5.
    assert u1 == pytest.approx(7.0 - 19.0 * 0.5)
    assert u2 == pytest.approx(u1)


def test_the_sheet_metal_metric_requires_the_flat_length():
    from scripts.build_verify_corpus import needed_dimensions

    needed = needed_dimensions(_channel())
    # 15 + 40 + 12 + 2 · π/2 · (2 + 0,5 · 2) = 76,4
    assert 76.4 in needed["lengths"]
    assert {19.0, 48.0, 16.0, 2.0, 50.0} <= set(needed["lengths"])


# ── Сварной узел (X3) ────────────────────────────────────────────────────────


def _weldment() -> dict:
    from app.ai.verify_corpus.synth import synth_spec

    return synth_spec("weldment", 9)  # основание 80×120×8, два ребра Т3 △4


def test_a_welded_part_is_drawn_front_plan_and_left_view():
    """Раньше узел шёл классом «пластина» по первому телу: рёбра без размеров,
    швов на листе нет, а вид `side` ядра смотрит СНИЗУ — рёбер не видно."""
    from app.ai.cad_ir.sheet_from_solid import classify_part, plan_views

    spec = _weldment()
    assert classify_part(spec, {}) == "weldment"
    views = plan_views("weldment", spec)
    assert [view["kind"] for view in views] == ["front", "plan", "top"]


def test_every_plate_of_the_weldment_is_dimensioned_on_its_view():
    from app.ai.cad_ir.sheet_from_solid import plan_sheet
    from app.ai.cad_ir.weldment_sheet import weldment_dimensions

    spec = _weldment()
    plan = plan_sheet(spec, {"bounds_mm": {"x": 80, "y": 120, "z": 38}})
    ratio = plan.ratio
    box = {"u_min": 0.0, "u_max": 0.0, "v_min": 0.0, "v_max": 0.0}
    drawing = {
        "views": [
            {"bounds_mm": {**box, "u_max": 80 * ratio, "v_max": 38 * ratio}},
            {"bounds_mm": {**box, "u_max": 80 * ratio, "v_max": 120 * ratio}},
            {"bounds_mm": {**box, "u_max": 120 * ratio, "v_max": 38 * ratio}},
        ],
        "dimensions": [],
    }

    weldment_dimensions(drawing, spec, plan)

    by = {}
    for item in drawing["dimensions"]:
        by.setdefault(item["measured_by"], []).append(item["value_mm"])
    assert by["weldment_width"] == [80.0]
    assert by["weldment_depth"] == [120.0]
    assert by["weldment_thickness"] == [8.0]
    assert sorted(by["weldment_rib_height"]) == [30.0, 30.0]
    assert sorted(by["weldment_rib_thickness"]) == [5.0, 6.0]
    # Положение ребра — от кромки основания до его ближней стенки.
    assert sorted(by["weldment_rib_position"]) == [26.0, 65.0]
    # Размер лежит на своём виде в масштабе листа: глубина основания на виде слева.
    depth = next(i for i in drawing["dimensions"] if i["measured_by"] == "weldment_depth")
    (u1, _), (u2, _) = depth["anchors_mm"]
    assert abs(u2 - u1) == pytest.approx(120 * ratio)


def test_a_weld_is_designated_by_standard_type_and_leg():
    from app.ai.cad_ir.weldment_sheet import weld_designation

    assert (
        weld_designation({"standard": "ГОСТ 5264-80", "designation": "Т3", "leg_mm": 4.0})
        == "ГОСТ 5264-80-Т3-△4"
    )


def test_the_weldment_metric_requires_every_plate_and_its_position():
    from scripts.build_verify_corpus import needed_dimensions

    needed = needed_dimensions(_weldment())
    assert needed["lengths"] == sorted([80.0, 120.0, 8.0, 30.0, 5.0, 65.0, 30.0, 6.0, 26.0])
