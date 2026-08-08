"""Model 2: parametric drafter (spec -> clean CadIR), no VLM needed."""

from __future__ import annotations

import pytest

from app.ai.cad_recognize.spec_vectorize import (
    SHEET_FRAME_EVIDENCE,
    EngineeringDrawingSpec,
    _dsl_to_ir,
    _num,
    _parse_spec_json,
    _spec_images,
    _whole_sheet_reader_schema,
    choose_standard_scale,
    draft_from_spec_async,
    draft_prismatic_body,
    draft_rotation_body,
    read_description_spec,
)


def test_whole_sheet_reader_schema_omits_fragment_owned_audit_payload():
    schema = _whole_sheet_reader_schema()

    assert "main_view" in schema["properties"]
    assert "views" in schema["properties"]
    assert "dimensions" not in schema["properties"]
    assert "annotations" not in schema["properties"]
    assert "title_block" not in schema["properties"]

    def property_names(value):
        if isinstance(value, dict):
            yield from value.get("properties", {}).keys()
            for child in value.values():
                yield from property_names(child)
        elif isinstance(value, list):
            for child in value:
                yield from property_names(child)

    names = set(property_names(schema))
    assert "evidence" not in names
    assert "features" not in names


def test_num_reads_values_from_messy_fields():
    assert _num(30) == 30.0
    assert _num("Ø30h6") == 30.0
    assert _num("±0,0095") == 0.0095
    assert _num(None) is None
    assert _num("no number") is None


def test_parse_spec_strips_think_and_fences():
    raw = '<think>reasoning</think>\n```json\n{"part":"Вал"}\n```'
    assert _parse_spec_json(raw) == {"part": "Вал"}
    assert _parse_spec_json("garbage") == {}


def test_draft_rotation_body_builds_clean_stepped_profile():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 80, "length_mm": 200},
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 100},
            ],
        }
    }
    ir = draft_rotation_body(spec)
    assert ir is not None
    segs = [e for e in ir.entities if e.type == "segment"]
    # Clean, not fragmented, but still inferred until source evidence is
    # independently checked or a human confirms it.
    assert 6 <= len(segs) <= 20
    assert all(s.origin == "spec" and s.assurance == "inferred" for s in segs)
    assert any(s.line_class == "axis" for s in segs)  # centreline
    assert ir.recognizer_used == "spec-drafter-rotation"


def test_draft_rotation_body_declines_when_no_sections():
    assert draft_rotation_body({"main_view": {"features": [{"kind": "hole", "diameter_mm": 10}]}}) is None


def test_draft_rotation_body_never_invents_missing_lengths():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": None},
            ],
        }
    }
    assert draft_rotation_body(spec) is None


def test_engineering_spec_marks_incomplete_rotation_profile_unresolved():
    spec = EngineeringDrawingSpec.model_validate({
        "schema_version": 1,
        "part": "Вал",
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": None},
            ],
        },
    })
    assert "body:0:outer:1:length-missing" in spec.unresolved


def test_engineering_spec_marks_prismatic_body_without_profile_unresolved():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {"type": "призматическая пластина"},
    })
    assert "body:0:profile-missing" in spec.unresolved


def test_optional_metadata_does_not_block_complete_geometry():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {
            "type": "призматическая пластина",
            "profile": {"shape": "rectangle", "width_mm": 120, "height_mm": 80},
        },
        "unresolved": ["материал детали не указан", "масштаб чертежа не указан"],
    })
    assert spec.unresolved == []
    assert len(spec.optional_unresolved) == 2


def test_spec_images_preserve_source_resolution_in_tiles():
    from PIL import Image

    images, descriptions, coverage = _spec_images(Image.new("RGB", (2484, 1758), "white"))
    assert len(images) > 1
    assert descriptions[0] == "image 0: overview 0,0,2484,1758"
    assert any("2484" in description for description in descriptions[1:])
    assert coverage == 1.0


@pytest.mark.parametrize(("columns", "rows"), [(8, 3), (9, 2), (12, 2), (4, 4), (3, 5)])
def test_tile_budget_never_drops_a_band_of_the_sheet(columns, rows):
    """A thinned grid must not leave a horizontal or vertical band unseen.

    The old rule picked evenly spaced entries of the ROW-MAJOR list, and on wide
    sheets that misses whole COLUMNS — measured: 8x3 kept 6 columns of 8, 9x2 and
    12x2 kept 8 of 9 and 8 of 12. A vertical band of the drawing was never shown
    to the reader, and nothing reported it.
    """
    from app.ai.cad_recognize.spec_vectorize import _tile_budget_boxes

    grid = [(c, r, c + 1, r + 1) for r in range(rows) for c in range(columns)]
    kept = _tile_budget_boxes(grid, columns, rows, 8)

    assert len(kept) <= 8
    assert len({box[1] for box in kept}) == min(rows, 8)
    assert len({box[0] for box in kept}) == min(columns, 8)


def test_a_sheet_over_the_tile_budget_says_so():
    """A partially shown sheet must announce it — silence reads as full coverage."""
    from PIL import Image

    images, descriptions, coverage = _spec_images(Image.new("RGB", (5000, 4000), "white"))
    boxes = [
        tuple(int(value) for value in description.split("bbox ")[1].split(","))
        for description in descriptions[1:]
        if "bbox " in description
    ]
    assert len(boxes) == 8
    assert len(images) == len(boxes) + 1
    assert 0.0 < coverage < 1.0
    assert any("из 16 фрагментов" in description for description in descriptions)


def test_draft_multiple_rotation_bodies_each_with_own_axis():
    spec = {
        "parts": [
            {"name": "Вал 1", "type": "тело вращения", "features": [
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 40},
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 120},
            ]},
            {"name": "Вал 2", "type": "тело вращения", "features": [
                {"kind": "cylinder", "diameter_mm": 20, "length_mm": 60},
                {"kind": "cylinder", "diameter_mm": 40, "length_mm": 80},
            ]},
        ]
    }
    ir = draft_rotation_body(spec, sheet_format="A3", landscape=True)
    assert ir is not None
    axes = [e for e in ir.entities if e.type == "segment" and e.line_class == "axis"]
    assert len(axes) == 2  # one constructed axis per body
    # The two axes are at different heights (bodies stacked, not overlapping).
    ys = sorted(a.p1.y for a in axes)
    assert ys[1] - ys[0] > 1.0
    # Each profile is exactly symmetric about its axis (top/bottom mirror).
    assert ir.sheet.format == "A3"


def test_choose_standard_scale_reduces_enlarges_and_fits():
    # A big part reduces to a standard reduction that fits the frame.
    assert choose_standard_scale(300, 80, "A4", landscape=True) == (0.5, "1:2")
    # A tiny part enlarges.
    ratio, label = choose_standard_scale(20, 10, "A4", landscape=True)
    assert label.endswith(":1") and ratio > 1
    # Only standard ratios are ever returned.
    assert label in {"2:1", "2.5:1", "4:1", "5:1", "10:1", "20:1", "40:1", "50:1", "100:1"}


def test_draft_rotation_body_lays_out_on_sheet_with_auto_scale():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 80, "length_mm": 200},
            ],
        }
    }
    ir = draft_rotation_body(spec, sheet_format="A3", landscape=True)
    assert ir is not None
    assert ir.sheet.format == "A3"
    assert ir.scale_source == "sheet_format"
    # A standard scale label was written to the title block.
    assert ir.sheet.title_block.get("scale") in {"1:1", "1:2", "1:2.5"}
    # Canvas equals the A3 landscape sheet at 4 px/mm (420×297 → 1680×1188).
    assert ir.source.image_width == 1680 and ir.source.image_height == 1188


def test_prismatic_plate_drafter_emits_exact_geometry_dimensions_and_holes():
    spec = {
        "main_view": {
            "type": "призматическая пластина",
            "profile": {
                "shape": "rectangle",
                "width_mm": 120,
                "height_mm": 80,
                "thickness_mm": 10,
                "holes": [
                    {"center_x_mm": -45, "center_y_mm": 25, "diameter_mm": 10, "tolerance": "H7"},
                    {"center_x_mm": 45, "center_y_mm": -25, "diameter_mm": 10},
                ],
            },
        },
    }
    ir = draft_prismatic_body(spec, sheet_format="A3")
    assert ir is not None
    assert ir.recognizer_used == "spec-drafter-prismatic"
    assert ir.sheet.format == "A3"
    assert len([entity for entity in ir.entities if entity.type == "circle"]) == 2
    values = [
        entity.value_mm for entity in ir.entities if entity.type == "dimension"
    ]
    assert sorted(values) == [10, 10, 80, 120]
    assert all(entity.origin == "spec" for entity in ir.entities)
    # Part geometry is constraint-validated; the ГОСТ frame/stamp around it is
    # sheet furniture and is tagged as such, not claimed as validated geometry.
    part = [
        entity for entity in ir.entities
        if SHEET_FRAME_EVIDENCE not in entity.evidence
    ]
    assert all(entity.assurance == "constraint_validated" for entity in part)
    frame = [
        entity for entity in ir.entities
        if SHEET_FRAME_EVIDENCE in entity.evidence
    ]
    assert frame and all(entity.assurance == "inferred" for entity in frame)


def test_prismatic_drafter_emits_four_true_corner_arcs_for_a_rounded_plate():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {
            "type": "призматическая пластина",
            "profile": {
                "shape": "rectangle",
                "width_mm": 120,
                "height_mm": 80,
                "corner_radius_mm": 10,
            },
        },
    }).model_dump(mode="json")

    ir = draft_prismatic_body(spec, px_per_mm=2)

    assert ir is not None
    corner_arcs = [entity for entity in ir.entities if entity.type == "arc"]
    assert len(corner_arcs) == 4
    assert {arc.radius for arc in corner_arcs} == {20.0}


def test_prismatic_profile_rejects_a_corner_radius_larger_than_half_side():
    with pytest.raises(ValueError, match="corner_radius_mm"):
        EngineeringDrawingSpec.model_validate({
            "main_view": {
                "type": "призматическая пластина",
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 100,
                    "height_mm": 40,
                    "corner_radius_mm": 21,
                },
            },
        })


def test_prismatic_drafter_declines_incomplete_profile():
    assert draft_prismatic_body({
        "main_view": {
            "type": "призматическая",
            "profile": {"shape": "rectangle", "width_mm": 120},
        },
    }) is None


def test_bolt_circle_expands_to_exact_holes_and_pitch_dimension():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {
            "type": "круглый фланец",
            "profile": {
                "shape": "circle",
                "diameter_mm": 180,
                "hole_patterns": [{
                    "kind": "bolt_circle",
                    "count": 6,
                    "bolt_circle_diameter_mm": 140,
                    "hole_diameter_mm": 14,
                    "start_angle_deg": 0,
                    "tolerance": "H7",
                }],
            },
        },
    }).model_dump(mode="json")
    ir = draft_prismatic_body(spec, px_per_mm=2)
    assert ir is not None
    circles = [entity for entity in ir.entities if entity.type == "circle"]
    assert len(circles) == 7
    values = sorted(
        entity.value_mm for entity in ir.entities if entity.type == "dimension"
    )
    assert values == [14, 14, 14, 14, 14, 14, 140, 180]
    flange_center = circles[0].center
    first_hole = circles[1].center
    assert first_hole.x - flange_center.x == pytest.approx(140)
    assert first_hole.y == pytest.approx(flange_center.y)


def test_capsule_slot_emits_two_exact_lines_two_arcs_and_dimensions():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {
            "type": "призматическая пластина",
            "profile": {
                "shape": "rectangle",
                "width_mm": 100,
                "height_mm": 60,
                "slots": [{
                    "center_x_mm": 0,
                    "center_y_mm": 0,
                    "length_mm": 40,
                    "width_mm": 12,
                    "rotation_deg": 30,
                }],
            },
        },
    }).model_dump(mode="json")
    ir = draft_prismatic_body(spec, px_per_mm=4)
    assert ir is not None
    assert len([entity for entity in ir.entities if entity.type == "arc"]) == 2
    assert len([entity for entity in ir.entities if entity.type == "segment"]) == 8
    values = sorted(
        entity.value_mm for entity in ir.entities if entity.type == "dimension"
    )
    assert values == [12, 40, 60, 100]


def test_prismatic_drafter_fails_closed_for_feature_outside_profile():
    spec = {
        "main_view": {
            "type": "призматическая пластина",
            "profile": {
                "shape": "rectangle",
                "width_mm": 100,
                "height_mm": 60,
                "holes": [{
                    "center_x_mm": 49,
                    "center_y_mm": 0,
                    "diameter_mm": 10,
                }],
            },
        },
    }
    assert draft_prismatic_body(spec) is None


def test_spec_rejects_slot_with_length_below_width():
    with pytest.raises(ValueError):
        EngineeringDrawingSpec.model_validate({
            "main_view": {
                "type": "призматическая пластина",
                "profile": {
                    "shape": "rectangle",
                    "width_mm": 100,
                    "height_mm": 60,
                    "slots": [{
                        "center_x_mm": 0,
                        "center_y_mm": 0,
                        "length_mm": 10,
                        "width_mm": 12,
                    }],
                },
            },
        })


def test_dsl_to_ir_decodes_all_primitive_kinds():
    ir = _dsl_to_ir({
        "lines": [[0, 0, 100, 0], [100, 0, 100, 50]],
        "circles": [[50, 25, 10]],
        "arcs": [[50, 25, 20, 0, 90]],
        "polylines": [{"pts": [[0, 0], [10, 10], [20, 0]], "closed": 1}],
    })
    assert ir is not None
    kinds = sorted(e.type for e in ir.entities)
    assert kinds == ["arc", "circle", "polyline", "segment", "segment"]
    assert ir.recognizer_used == "spec-drafter-generative"
    assert _dsl_to_ir({"lines": [], "circles": [], "arcs": [], "polylines": []}) is None


class _FakeResp:
    def __init__(self, text):
        self.text = text


class _FakeRouter:
    """Stand-in Model 2: returns a fixed geometry DSL, records the request."""

    def __init__(self, text):
        self._text = text
        self.seen = None

    async def run(self, request):
        self.seen = request
        return _FakeResp(self._text)


@pytest.mark.asyncio
async def test_description_reader_accepts_valid_ready_json_without_model():
    router = _FakeRouter("should not be used")
    spec = await read_description_spec(
        '{"schema_version":1,"part":"Плита","main_view":{"type":"призматическая пластина","profile":{"shape":"rectangle","width_mm":120,"height_mm":80,"thickness_mm":10,"holes":[]}}}',
        router=router,
    )
    assert spec["main_view"]["profile"]["width_mm"] == 120
    assert router.seen is None


@pytest.mark.asyncio
async def test_description_reader_uses_local_cad_reader_for_free_text():
    router = _FakeRouter(
        '{"schema_version":1,"part":"Плита","main_view":{"type":"призматическая пластина","profile":{"shape":"rectangle","width_mm":120,"height_mm":80,"thickness_mm":10,"holes":[]}}}'
    )
    spec = await read_description_spec("Пластина 120 на 80", router=router)
    assert spec["main_view"]["profile"]["height_mm"] == 80
    assert router.seen.task.value == "cad_spec_read"
    assert router.seen.confidential is True
    assert router.seen.allow_cloud is False


@pytest.mark.asyncio
async def test_description_reader_does_not_block_on_unrequested_tolerances_or_rounding():
    router = _FakeRouter(
        '{"schema_version":1,"part":"Плита","main_view":{"type":"призматическая пластина",'
        '"profile":{"shape":"rectangle","width_mm":100,"height_mm":60,"slots":['
        '{"center_x_mm":0,"center_y_mm":0,"length_mm":40,"width_mm":12}]}},'
        '"unresolved":["допуски на габаритные размеры",'
        '"радиусы скруглений углов пластины"]}'
    )
    spec = await read_description_spec(
        "Прямоугольная пластина 100×60 с пазом 40×12 в центре", router=router
    )
    assert spec["unresolved"] == []
    assert len(spec["optional_unresolved"]) == 2


@pytest.mark.asyncio
async def test_description_reader_keeps_explicitly_requested_missing_radius_blocking():
    router = _FakeRouter(
        '{"schema_version":1,"part":"Плита","main_view":{"type":"призматическая пластина",'
        '"profile":{"shape":"rectangle","width_mm":100,"height_mm":60}},'
        '"unresolved":["радиус скругления явно запрошен, но не указан"]}'
    )
    spec = await read_description_spec(
        "Пластина 100×60 со скруглёнными углами, радиус не указан", router=router
    )
    assert spec["unresolved"] == ["радиус скругления явно запрошен, но не указан"]


@pytest.mark.asyncio
async def test_rotation_body_uses_constructed_axis_not_generative():
    # Deterministic-first: a rotation body's axis is CONSTRUCTED, so the model
    # (which mis-places the axis) is not consulted at all.
    router = _FakeRouter('{"lines":[[0,0,100,0]],"circles":[],"arcs":[],"polylines":[]}')
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 100},
            ],
        }
    }
    ir = await draft_from_spec_async(spec, draft_model="apex", router=router)
    assert ir is not None
    assert ir.recognizer_used == "spec-drafter-rotation"
    assert router.seen is None  # generative model never called for a rotation body


@pytest.mark.asyncio
async def test_prismatic_part_uses_generative_model():
    # A non-rotation part: the parametric drafter declines → generative model.
    router = _FakeRouter('{"lines":[[0,0,120,0],[120,0,120,60],[120,60,0,60],[0,60,0,0]],"circles":[[60,30,8]],"arcs":[],"polylines":[]}')
    spec = {"main_view": {"type": "призматическая", "features": [{"kind": "plate"}]}}
    ir = await draft_from_spec_async(spec, draft_model="apex", router=router)
    assert ir is not None
    assert ir.recognizer_used == "spec-drafter-generative"
    assert router.seen.preferred_model == "apex"
    assert router.seen.confidential is True and router.seen.allow_cloud is False


@pytest.mark.asyncio
async def test_no_model_assigned_uses_deterministic():
    spec = {
        "main_view": {
            "type": "тело вращения (вал)",
            "features": [
                {"kind": "cylinder", "diameter_mm": 50, "length_mm": 150},
                {"kind": "cylinder", "diameter_mm": 30, "length_mm": 100},
            ],
        }
    }
    ir = await draft_from_spec_async(spec, draft_model=None)
    assert ir is not None and ir.recognizer_used == "spec-drafter-rotation"


def test_layout_on_sheet_scales_generative_geometry():
    from app.ai.cad_recognize.spec_vectorize import _dsl_to_ir, _layout_on_sheet
    ir = _dsl_to_ir({"lines": [[0, 0, 100, 0], [100, 0, 100, 50], [0, 50, 100, 50]],
                     "circles": [], "arcs": [], "polylines": []})
    spec = {"dimensions": [{"value": "300"}, {"value": "150"}]}
    _layout_on_sheet(ir, spec, "A3", True)
    assert ir.sheet.format == "A3"
    assert ir.scale_source == "sheet_format"
    assert ir.source.image_width == 1680 and ir.source.image_height == 1188
    # geometry moved into the sheet frame (positive, within canvas)
    xs = [e.p1.x for e in ir.entities if e.type == "segment"]
    assert all(0 < x < 1680 for x in xs)


# --- Views (ГОСТ 2.305 projection alignment) --------------------------------


def _shaft_spec_with_side_view() -> dict:
    return {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": 60},
            ],
        },
        "views": [{"kind": "side", "body_index": 0, "label": "Вид слева"}],
    }


def test_requested_side_view_is_drafted_as_concentric_circles():
    from app.ai.cad_ir.schema import Circle

    ir = draft_rotation_body(_shaft_spec_with_side_view(), px_per_mm=4.0)
    assert ir is not None
    circles = [e for e in ir.entities if isinstance(e, Circle)]
    # One circle per distinct outer diameter, and nothing invented.
    assert len(circles) == 2
    radii = sorted(round(c.radius, 3) for c in circles)
    assert radii == [30 * 4.0 / 2, 50 * 4.0 / 2]


def test_side_view_shares_the_front_view_axis_exactly():
    """Projection alignment must be constructed, not approximated."""
    from app.ai.cad_ir.schema import Circle, Segment

    ir = draft_rotation_body(_shaft_spec_with_side_view(), px_per_mm=4.0)
    assert ir is not None
    circles = [e for e in ir.entities if isinstance(e, Circle)]
    axes = [
        e for e in ir.entities
        if isinstance(e, Segment) and e.line_class == "axis"
        and abs(e.p1.y - e.p2.y) < 1e-9
    ]
    assert axes, "front view must carry a centreline"
    front_axis_y = axes[0].p1.y
    assert all(abs(c.center.y - front_axis_y) < 1e-9 for c in circles)
    # And the left view is to the RIGHT of the front view (ГОСТ 2.305).
    front_right = max(
        max(e.p1.x, e.p2.x)
        for e in ir.entities
        if isinstance(e, Segment) and e.line_class == "contour"
    )
    assert all(c.center.x > front_right for c in circles)


def test_no_views_declared_keeps_the_front_view_only():
    from app.ai.cad_ir.schema import Circle

    spec = _shaft_spec_with_side_view()
    spec.pop("views")
    ir = draft_rotation_body(spec, px_per_mm=4.0)
    assert ir is not None
    assert not [e for e in ir.entities if isinstance(e, Circle)]


def test_side_view_enters_the_sheet_scale_decision():
    """A part that fits A4 alone must scale down once a left view is added."""
    wide = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 60, "length_mm": 120},
                {"diameter_mm": 45, "length_mm": 30},
            ],
        },
    }
    without = draft_rotation_body(dict(wide), sheet_format="A4", landscape=True)
    with_side = draft_rotation_body(
        {**wide, "views": [{"kind": "side", "body_index": 0}]},
        sheet_format="A4",
        landscape=True,
    )
    assert without is not None and with_side is not None
    # Same sheet, smaller drawn size: mm-per-px grows when the scale shrinks.
    assert with_side.scale > without.scale
    assert with_side.source.image_width == without.source.image_width


def test_view_naming_a_missing_body_blocks_the_spec():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {"type": "тело вращения (вал)", "outer": [
            {"diameter_mm": 30, "length_mm": 40},
            {"diameter_mm": 50, "length_mm": 60},
        ]},
        "views": [{"kind": "side", "body_index": 7}],
    })
    assert "view:0:body-index-out-of-range" in spec.unresolved


def test_unsectioned_bore_is_dashed_in_both_views():
    """ГОСТ 2.303: an invisible bore is a hidden line, never a contour."""
    from app.ai.cad_ir.schema import Circle, Segment

    spec = _shaft_spec_with_side_view()
    spec["main_view"]["bore"] = [{"diameter_mm": 16, "length_mm": 100}]
    ir = draft_rotation_body(spec, px_per_mm=4.0)
    assert ir is not None
    hidden_segments = [
        e for e in ir.entities
        if isinstance(e, Segment) and e.line_class == "hidden"
    ]
    assert hidden_segments, "front view must draw the bore as hidden lines"
    bore_circles = [
        e for e in ir.entities
        if isinstance(e, Circle) and e.line_class == "hidden"
    ]
    assert len(bore_circles) == 1
    assert bore_circles[0].radius == pytest.approx(16 * 4.0 / 2)


# --- Section, sheet frame, and honest dimension text ------------------------


def _hollow_spec(views: list[dict]) -> dict:
    return {
        "part": "Втулка",
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": 60},
            ],
            "bore": [{"diameter_mm": 16, "length_mm": 100}],
        },
        "views": views,
    }


def test_section_hatches_the_wall_and_makes_bore_edges_solid():
    from app.ai.cad_ir.schema import HatchRegion, Segment

    ir = draft_rotation_body(
        _hollow_spec([{"kind": "section", "body_index": 0}]), px_per_mm=4.0
    )
    assert ir is not None
    hatches = [e for e in ir.entities if isinstance(e, HatchRegion)]
    # One wall band each side of the axis.
    assert len(hatches) == 2
    assert all(len(h.boundary) >= 4 for h in hatches)
    # A cut edge is a contour, never a hidden line.
    assert not [
        e for e in ir.entities
        if isinstance(e, Segment) and e.line_class == "hidden"
    ]


def test_section_boundary_is_a_simple_loop_not_a_bowtie():
    """The return path must run right-to-left, or the fill self-intersects."""
    from app.ai.cad_ir.schema import HatchRegion

    ir = draft_rotation_body(
        _hollow_spec([{"kind": "section", "body_index": 0}]), px_per_mm=4.0
    )
    assert ir is not None
    loop = [e for e in ir.entities if isinstance(e, HatchRegion)][0].boundary
    xs = [p.x for p in loop]
    # Outward leg is non-decreasing in x, return leg non-increasing.
    turn = xs.index(max(xs))
    assert all(a <= b + 1e-9 for a, b in zip(xs[:turn], xs[1 : turn + 1]))
    assert all(a >= b - 1e-9 for a, b in zip(xs[turn:], xs[turn + 1 :]))


def test_section_without_a_bore_is_ignored():
    spec = _hollow_spec([{"kind": "section", "body_index": 0}])
    spec["main_view"].pop("bore")
    ir = draft_rotation_body(spec, px_per_mm=4.0)
    assert ir is not None
    assert not [e for e in ir.entities if e.type == "hatch"]


def test_sheet_gets_a_gost_frame_and_a_filled_stamp():
    spec = _hollow_spec([])
    spec["title_block"] = {"material": "Сталь 45", "designation": "АБВГ.001"}
    ir = draft_rotation_body(spec, sheet_format="A3", landscape=True)
    assert ir is not None
    assert ir.sheet.frame is True
    assert ir.sheet.title_block["designation"] == "АБВГ.001"
    assert ir.sheet.title_block["material"] == "Сталь 45"
    assert ir.sheet.title_block["name"] == "Втулка"
    # ГОСТ 2.302 prefers 1:1 — the drafter must not enlarge just because the
    # sheet has spare room.
    assert ir.sheet.title_block["scale"] == "1:1"
    frame = [
        e for e in ir.entities if SHEET_FRAME_EVIDENCE in e.evidence
    ]
    assert frame, "the frame must exist as real entities, not just a flag"


def test_drawing_never_overlaps_the_title_block_band():
    """The stamp band is reserved: nothing drafted may reach into it."""
    spec = _hollow_spec([])
    ir = draft_rotation_body(spec, sheet_format="A4", landscape=False)
    assert ir is not None
    sheet_h = ir.source.image_height
    stamp_top = sheet_h - 55.0 * 4.0
    part = [
        e for e in ir.entities
        if SHEET_FRAME_EVIDENCE not in e.evidence and e.type == "segment"
    ]
    assert part
    assert all(max(e.p1.y, e.p2.y) < stamp_top for e in part)


def test_dimension_text_reproduces_the_read_tolerance():
    spec = _hollow_spec([])
    spec["dimensions"] = [{"value": "Ø50js6"}, {"value": "40h11"}]
    ir = draft_rotation_body(spec, px_per_mm=4.0)
    assert ir is not None
    texts = [e.text for e in ir.entities if e.type == "dimension"]
    assert "Ø50js6" in texts, "a read fit must survive into the drawing"
    assert "40h11" in texts
    # A nominal the reader never wrote down stays a plain number, not a guess.
    assert "Ø30" in texts


def test_diameter_tolerance_never_leaks_onto_a_length():
    spec = _hollow_spec([])
    # Same nominal 40 as the first section's LENGTH, but read as a diameter.
    spec["dimensions"] = [{"value": "Ø40k6"}]
    ir = draft_rotation_body(spec, px_per_mm=4.0)
    assert ir is not None
    lengths = [
        e.text for e in ir.entities
        if e.type == "dimension" and e.kind == "linear" and e.value_mm == 40
    ]
    assert lengths == ["40"]


def test_source_sheet_scale_is_reproduced_when_it_fits():
    """Redrawing a 1:2 sheet must not silently return it as 1:1."""
    spec = _hollow_spec([])
    spec["title_block"] = {"scale": "1:2"}
    ir = draft_rotation_body(spec, sheet_format="A3", landscape=True)
    assert ir is not None
    assert ir.sheet.title_block["scale"] == "1:2"
    assert ir.scale == pytest.approx(1.0 / (0.5 * 4.0))


def test_unreadable_source_scale_falls_back_to_the_auto_choice():
    spec = _hollow_spec([])
    spec["title_block"] = {"scale": "приблизительно"}
    ir = draft_rotation_body(spec, sheet_format="A3", landscape=True)
    assert ir is not None
    assert ir.sheet.title_block["scale"] == "1:1"


# --- Reader robustness ------------------------------------------------------


def test_single_object_where_a_list_is_expected_is_not_discarded():
    """A live read of a real sheet was thrown away over this exact shape."""
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    parsed = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": 60},
            ],
            "bore": None,
            "profile": {
                "shape": "circle",
                "diameter_mm": 80,
                "holes": None,
                # The model emitted ONE object instead of a one-item list.
                "hole_patterns": {
                    "count": 6,
                    "bolt_circle_diameter_mm": 140,
                    "hole_diameter_mm": 14,
                },
            },
        },
        "views": {"kind": "side", "body_index": 0},
        "dimensions": None,
    }
    spec = EngineeringDrawingSpec.model_validate(_coerce_spec_containers(parsed))
    assert len(spec.main_view.outer) == 2
    assert spec.main_view.profile is not None
    assert len(spec.main_view.profile.hole_patterns) == 1
    assert [v.kind for v in spec.views] == ["side"]


def test_shape_repair_never_invents_a_missing_dimension():
    """Container repair must not weaken the fail-closed geometry contract."""
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    parsed = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50},  # length never read
            ],
        },
    }
    spec = EngineeringDrawingSpec.model_validate(_coerce_spec_containers(parsed))
    assert any("length-missing" in item for item in spec.unresolved)
    assert draft_rotation_body(spec.model_dump(mode="json"), px_per_mm=4.0) is None


def test_reader_format_repair_keeps_invalid_section_path_fail_closed():
    """Observed live: ratio had no discriminator and paths were flat pairs."""
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    parsed = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {
                    "diameter_mm": 56.55,
                    "length_mm": 40,
                    "taper": {"ratio": "7:24"},
                },
                {"diameter_mm": 50, "length_mm": 60},
            ],
        },
        "views": [{
            "kind": "section",
            "view_id": "А-А",
            "section_path_mm": [150, 270],
            "detail_scale_factor": None,
        }],
    }

    repaired = _coerce_spec_containers(parsed)
    spec = EngineeringDrawingSpec.model_validate(repaired)

    assert spec.main_view.outer[0].taper is not None
    assert spec.main_view.outer[0].taper.kind == "ratio"
    assert spec.views[0].section_path_mm == []
    assert spec.views[0].detail_scale_factor == 2.0
    assert any("путь сечения не подтверждён" in item for item in spec.unresolved)


def test_truncated_reader_json_is_reported_not_salvaged():
    """Closing the open braces would draft a silently shorter part."""
    from app.ai.cad_recognize.spec_vectorize import (
        SpecReadTruncatedError,
        _parse_spec_json,
    )

    # A real cut-off answer: inner objects closed, outer ones still open.
    cut = (
        '{"part": "Вал", "main_view": {"outer": ['
        '{"diameter_mm": 30, "length_mm": 40}, {"diameter_mm": 50, "leng'
    )
    # Lenient parsing (free-text path) keeps the old best-effort behaviour...
    assert _parse_spec_json(cut) == {}
    # ...but reading a real sheet must say the answer was cut off.
    with pytest.raises(SpecReadTruncatedError):
        _parse_spec_json(cut, strict=True)


def test_plain_garbage_is_not_reported_as_truncation():
    from app.ai.cad_recognize.spec_vectorize import (
        SpecReadMalformedError,
        _parse_spec_json,
    )

    # Nothing JSON-shaped at all is simply "no spec", not a cut-off answer.
    assert _parse_spec_json("no json here", strict=True) == {}
    # A complete-looking but broken object is a malformed answer, and saying so
    # beats a silent empty result the user cannot act on.
    with pytest.raises(SpecReadMalformedError):
        _parse_spec_json('{"a": [1, 2,]}', strict=True)


def test_malformed_evidence_never_discards_the_dimension_it_annotates():
    """Five real shaft sections were lost to a badly shaped citation."""
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    parsed = {
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40, "evidence": "Ø30 слева"},
                {
                    "diameter_mm": 50, "length_mm": 60,
                    "evidence": [{"image_index": "1", "bbox": [1, 2], "raw_text": "Ø50"}],
                },
            ],
        },
    }
    spec = EngineeringDrawingSpec.model_validate(_coerce_spec_containers(parsed))
    assert len(spec.main_view.outer) == 2
    assert spec.main_view.outer[0].evidence[0].raw_text == "Ø30 слева"
    # A bbox that is not four numbers is dropped, the reading is kept.
    assert spec.main_view.outer[1].evidence[0].bbox is None
    assert spec.main_view.outer[1].evidence[0].raw_text == "Ø50"


def test_mis_nested_json_is_not_reported_as_truncation():
    """A finished-but-mis-nested answer needs a different answer than a cut-off one."""
    from app.ai.cad_recognize.spec_vectorize import (
        SpecReadMalformedError,
        _parse_spec_json,
    )

    # Observed live: the reader opened a new object before a top-level field.
    mis_nested = '{"part":"Вал","main_view":{"outer":[]},{"views":[]}}'
    with pytest.raises(SpecReadMalformedError):
        _parse_spec_json(mis_nested, strict=True)


def test_object_closed_one_brace_early_is_rejoined_not_lost():
    """Observed live: '...}},"parts":[]...' — complete output split by a stray brace."""
    from app.ai.cad_recognize.spec_vectorize import _parse_spec_json

    body = (
        '{"part":"Вал","main_view":{"type":"вал","outer":['
        '{"diameter_mm":30,"length_mm":40},{"diameter_mm":50,"length_mm":60}]}}'
        ',"parts":[],"views":[{"kind":"side","body_index":0}]}'
    )
    parsed = _parse_spec_json(body, strict=True)
    assert parsed["part"] == "Вал"
    assert len(parsed["main_view"]["outer"]) == 2
    # The continuation the model actually wrote survives the repair.
    assert parsed["views"][0]["kind"] == "side"


def test_repair_never_invents_a_missing_tail():
    """A cut-off answer must still fail: there is nothing to re-join."""
    from app.ai.cad_recognize.spec_vectorize import (
        SpecReadTruncatedError,
        _parse_spec_json,
    )

    cut = '{"part":"Вал","main_view":{"outer":[{"diameter_mm":30,"length_mm":40}'
    with pytest.raises(SpecReadTruncatedError):
        _parse_spec_json(cut, strict=True)


def test_a_complete_flange_profile_is_not_blocked_by_a_wrong_type_label():
    """Live: a correctly read flange was rejected for being labelled a shaft."""
    spec = EngineeringDrawingSpec.model_validate({
        "part": "Фланец",
        "main_view": {
            # The reader's own (wrong) classification.
            "type": "тело вращения (вал)",
            "outer": [{"diameter_mm": 560, "length_mm": 20}],
            "profile": {
                "shape": "circle", "diameter_mm": 560, "thickness_mm": 20,
                "holes": [{"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": 80}],
            },
        },
    })
    assert spec.unresolved == []


def test_a_shaft_with_one_section_and_no_profile_still_blocks():
    spec = EngineeringDrawingSpec.model_validate({
        "main_view": {
            "type": "тело вращения (вал)",
            "outer": [{"diameter_mm": 50, "length_mm": 60}],
        },
    })
    assert any("outer-profile-incomplete" in item for item in spec.unresolved)


def test_an_object_in_unresolved_does_not_delete_the_geometry():
    """Live: a reason written as an object discarded a correctly read flange."""
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    parsed = {
        "main_view": {
            "type": "вал",
            "outer": [
                {"diameter_mm": 30, "length_mm": 40},
                {"diameter_mm": 50, "length_mm": 60},
            ],
        },
        "optional_unresolved": [{"field": "масштаб", "why": "не читается"}],
        "unresolved": [None, "габарит не найден"],
    }
    spec = EngineeringDrawingSpec.model_validate(_coerce_spec_containers(parsed))
    assert len(spec.main_view.outer) == 2
    assert any("масштаб" in item for item in spec.optional_unresolved)
    assert "габарит не найден" in spec.unresolved


def test_an_unusable_hole_pattern_is_dropped_not_the_whole_sheet():
    """A bearing-housing sheet scored 0/2, 0/1, 0/3 over one null diameter."""
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    parsed = {
        "main_view": {
            "type": "фланец",
            "profile": {
                "shape": "circle", "diameter_mm": 140, "thickness_mm": 20,
                "holes": [
                    {"center_x_mm": 0, "center_y_mm": 0, "diameter_mm": 40},
                    {"center_x_mm": 10, "center_y_mm": 0, "diameter_mm": None},
                ],
                "hole_patterns": [{
                    "kind": "bolt_circle", "count": 6,
                    "bolt_circle_diameter_mm": 110, "hole_diameter_mm": None,
                }],
            },
        },
    }
    spec = EngineeringDrawingSpec.model_validate(_coerce_spec_containers(parsed))
    profile = spec.main_view.profile
    assert profile is not None
    assert len(profile.holes) == 1
    assert profile.holes[0].diameter_mm == 40
    assert profile.hole_patterns == []


def test_drafter_reproduces_a_hand_written_spec_exactly():
    """The drafter is not the weak link — pin that so it stays true.

    ``example-drawings/detal_126_reference_spec.json`` is a hand-written spec
    for the spindle sheet: every number in it was read off the drawing by a
    human, so anything wrong downstream of it is the drafter's own defect.
    Measured, all nine sections come back to within 0.02 mm of what the spec
    stated, in the right order and at the right axial positions.

    Which means: when a digitized sheet is wrong, look at the reader or at the
    contract, not here.
    """
    import asyncio
    import json
    import pathlib
    from collections import defaultdict

    from app.ai.cad_recognize.spec_vectorize import draft_from_spec_async

    spec_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "fixtures" / "detal_126_reference_spec.json"
    )
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec.pop("_comment", None)

    ir = asyncio.run(draft_from_spec_async(spec))

    segments = [e for e in ir.entities if e.type == "segment"]
    horizontal = [e for e in segments if abs(e.p1.y - e.p2.y) < 0.5]
    ys = [p.y for e in segments for p in (e.p1, e.p2)]
    axis = (min(ys) + max(ys)) / 2
    body = [e for e in horizontal if max(e.p1.x, e.p2.x) < 800]
    x0 = min(min(e.p1.x, e.p2.x) for e in body)
    x1 = max(max(e.p1.x, e.p2.x) for e in body)
    overall = sum(s["length_mm"] for s in spec["main_view"]["outer"])
    mm_per_px = overall / (x1 - x0)

    drawn = []
    for entity in body:
        if entity.p1.y >= axis - 1:
            continue
        left, right = min(entity.p1.x, entity.p2.x), max(entity.p1.x, entity.p2.x)
        drawn.append((2 * (axis - entity.p1.y) * mm_per_px, (right - left) * mm_per_px))

    expected = [
        (s["diameter_mm"], s["length_mm"])
        for s in spec["main_view"]["outer"] + spec["main_view"]["bore"]
    ]
    for diameter, length in expected:
        assert any(
            abs(d - diameter) <= 0.02 and abs(l - length) <= 0.02 for d, l in drawn
        ), f"Ø{diameter} L{length} is not on the drawing"


def test_refusal_sheet_carries_the_reading_and_no_part_geometry():
    """What a fail-closed stop can still hand over.

    Refusing to build is right; handing over NOTHING threw away the work that
    was right along with the work that was wrong. The stamp, the requirements
    and the callouts were read and verified — only the geometry was not — and
    the user was left starting from a blank page.

    The sheet must carry those and must NOT carry a part: no contour, no
    circle, no step. A refusal that produced something mistakable for a part
    would be worse than the empty page it replaces.
    """
    from app.ai.cad_recognize.spec_vectorize import draft_sheet_without_geometry

    spec = {
        "part": "Шпиндель",
        "main_view": {"type": "тело вращения", "outer": [], "bore": []},
        "title_block": {"material": "Сталь 55 ГОСТ 1050-2013", "scale": "1:2"},
        "dimensions": [{"value": "Ø102h6"}, {"value": "470"}],
        "annotations": [{"kind": "hardness", "text": "HRC 58…62"}],
    }

    ir = draft_sheet_without_geometry(spec, sheet_format="A3")

    assert ir is not None
    texts = " ".join(e.text for e in ir.entities if e.type == "text")
    assert "Сталь 55 ГОСТ 1050-2013" in texts
    assert "HRC 58…62" in texts
    assert "Ø102h6" in texts and "470" in texts
    assert "Технические требования" in texts
    assert ir.scale == 0.25
    assert ir.scale_source == "sheet_format"
    assert ir.source.kind == "spec"

    # Sheet furniture is straight lines only — the frame and the stamp grid.
    # A curve or an arc could only have come from the part.
    assert not [e for e in ir.entities if e.type in ("circle", "arc", "polyline", "hatch")]


def test_coerce_marks_incomplete_profile_and_pattern_unresolved():
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    raw = {
        "main_view": {
            "type": "prismatic",
            "profile": {
                "shape": "rectangle",
                "width_mm": None,
                "height_mm": 80,
                "hole_patterns": [{
                    "kind": "bolt_circle",
                    "count": 6,
                    "bolt_circle_diameter_mm": 0,
                    "hole_diameter_mm": 8,
                }],
            },
        },
        "unresolved": [],
    }

    repaired = _coerce_spec_containers(raw)

    assert repaired["main_view"]["profile"] is None
    assert any(
        item.startswith("geometry_input_incomplete:main_view.profile.hole_patterns.0")
        for item in repaired["unresolved"]
    )
    assert any(
        item.startswith("geometry_input_incomplete:main_view.profile:")
        for item in repaired["unresolved"]
    )


def test_coerce_keeps_complete_profile_and_pattern():
    from app.ai.cad_recognize.spec_vectorize import _coerce_spec_containers

    raw = {
        "main_view": {
            "profile": {
                "shape": "circle",
                "diameter_mm": 100,
                "hole_patterns": [{
                    "kind": "bolt_circle",
                    "count": 6,
                    "bolt_circle_diameter_mm": 70,
                    "hole_diameter_mm": 8,
                }],
            },
        },
    }

    repaired = _coerce_spec_containers(raw)

    assert repaired["main_view"]["profile"]["diameter_mm"] == 100
    assert len(repaired["main_view"]["profile"]["hole_patterns"]) == 1
