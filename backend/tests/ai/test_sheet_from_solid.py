"""The sheet built from the solid: what goes on it, and where.

No kernel here — the projections are recorded fixtures shaped exactly like
``/drawing`` answers, so these tests are about the decisions: which views a part
gets, which sheet and scale, which edges become dimensions, and what the drawn
result claims about itself. The live half lives in scripts/cad_kernel_smoke.py.
"""

from __future__ import annotations

from app.ai.cad_ir.sheet_from_solid import (
    _dimension_requests,
    _label_dimensions,
    classify_part,
    plan_sheet,
    plan_views,
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
        {"value": "Ø80js6"}, {"value": "Ø102"}, {"value": "Ø60"},
        {"value": "150"}, {"value": "120"}, {"value": "470"},
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
    """"top" was read, validated, and then silently never drawn."""
    spec = {**_SHAFT, "views": [{"kind": "top", "body_index": 0}]}
    assert "top" in [view["kind"] for view in plan_views("solid_rotation", spec)]


def test_a_shaft_with_cross_features_gets_the_end_view_that_shows_them():
    spec = {
        "main_view": {
            **_SHAFT["main_view"],
            "keyways": [{"axial_start_mm": 20.0, "length_mm": 85.0,
                         "width_mm": 12.0, "depth_mm": 5.0}],
        }
    }
    assert "side" in [view["kind"] for view in plan_views("solid_rotation", spec)]


def test_the_scale_is_standard_and_the_sheet_is_the_smallest_that_reads():
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    assert plan.scale_label in {"1:1", "1:2", "1:2.5", "1:4", "1:5"}
    assert plan.sheet_format in {"A4", "A3"}
    # A part is not enlarged onto a bigger sheet just because it would fit.
    assert plan.ratio <= 1.0


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
                "type": "line", "edge_index": index,
                "points": [[u0, v], [u1, v]],
            }
            for index, v, u0, u1 in lines
        ] + [
            {"type": "circle", "edge_index": index, "center": [0.0, 0.0], "radius": r}
            for index, r in circles
        ],
        "hidden": [],
        "hatch": [],
    }


# Measured off a real 1:2.5 section of the shaft above: each step's two
# generatrices, mirrored about the axis. Note the upper one comes SECOND for
# two of the three steps — the case that used to lose those diameters.
_SECTION = _view("section", [
    (3, 16.0, -94.0, -34.0), (4, -16.0, -94.0, -34.0),
    (6, -20.4, -34.0, 46.0), (8, -12.0, 46.0, 94.0),
    (14, 12.0, 46.0, 94.0), (16, 20.4, -34.0, 46.0),
])


def test_every_step_diameter_is_dimensioned_whichever_generatrix_comes_first():
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    plan.ratio, plan.scaffold_views = 0.4, set()
    requests = _dimension_requests({"views": [_SECTION]}, _SHAFT, plan)

    diameters = sorted(
        request["_nominal_mm"] for request in requests if request["_is_diameter"]
    )
    assert diameters == [60.0, 80.0, 102.0]
    # A diameter on a longitudinal view is measured between BOTH generatrices.
    assert all(
        "second_edge_index" in request
        for request in requests if request["_is_diameter"]
    )


def test_the_bore_is_dimensioned_too():
    plan = plan_sheet(_HOLLOW, _SHAFT_REPORT)
    plan.ratio, plan.scaffold_views = 0.4, set()
    section = _view("section", [
        (3, 16.0, -94.0, -34.0), (4, -16.0, -94.0, -34.0),
        (10, -8.0, -94.0, 94.0), (11, 8.0, -94.0, 94.0),
    ])
    requests = _dimension_requests({"views": [section]}, _HOLLOW, plan)
    assert 40.0 in [request["_nominal_mm"] for request in requests]


def test_the_dimension_chain_is_left_open():
    """ГОСТ 2.307 forbids closing it: the longest step goes undimensioned and
    the overall length carries it."""
    plan = plan_sheet(_SHAFT, _SHAFT_REPORT)
    plan.ratio, plan.scaffold_views = 0.4, set()
    lengths_view = _view("front", [
        (1, 16.0, -94.0, -34.0),   # 150 mm step, at 1:2.5 -> 60 mm
        (5, 20.4, -34.0, 46.0),    # 200 mm step -> 80 mm (the longest: skipped)
        (6, 12.0, 46.0, 94.0),     # 120 mm step -> 48 mm
    ])
    requests = _dimension_requests({"views": [lengths_view]}, _SHAFT, plan)
    lengths = sorted(
        request["_nominal_mm"] for request in requests if not request["_is_diameter"]
    )
    assert 200.0 not in lengths
    assert lengths == [120.0, 150.0]


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
