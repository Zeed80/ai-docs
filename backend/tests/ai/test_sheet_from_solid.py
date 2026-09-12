"""The sheet built from the solid: what goes on it, and where.

No kernel here — the projections are recorded fixtures shaped exactly like
``/drawing`` answers, so these tests are about the decisions: which views a part
gets, which sheet and scale, which edges become dimensions, and what the drawn
result claims about itself. The live half lives in scripts/cad_kernel_smoke.py.
"""

from __future__ import annotations

import io

import ezdxf

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
