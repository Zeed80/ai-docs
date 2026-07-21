from app.ai.cad_entity_metrics import compare_ir
from app.ai.cad_ir.schema import (
    AnnotationEntity,
    CadIR,
    Circle,
    DimensionEntity,
    HatchRegion,
    Point,
    Segment,
    SourceInfo,
    TextEntity,
)


def _ir(entities):
    return CadIR(
        source=SourceInfo(image_width=1000, image_height=500),
        scale=1.0,
        scale_source="manual",
        entities=entities,
    )


def test_exact_entities_are_an_exact_sheet():
    truth = _ir([
        Segment(p1=Point(x=10, y=20), p2=Point(x=900, y=20)),
        Circle(center=Point(x=300, y=200), radius=50),
        TextEntity(position=Point(x=50, y=80), text="Ø18H7"),
    ])
    predicted = CadIR.model_validate(truth.model_dump())

    result = compare_ir(predicted, truth)

    assert result["exact_sheet"] is True
    assert result["micro"]["f1"] == 1.0


def test_pixel_like_geometry_without_text_is_not_exact():
    truth = _ir([
        Segment(p1=Point(x=10, y=20), p2=Point(x=900, y=20)),
        TextEntity(position=Point(x=50, y=80), text="Ø18H7"),
    ])
    predicted = _ir([
        Segment(p1=Point(x=10, y=20), p2=Point(x=900, y=20)),
    ])

    result = compare_ir(predicted, truth)

    assert result["exact_sheet"] is False
    assert result["per_type"]["text"]["false_negative"] == 1
    assert result["micro"]["recall"] == 0.5


def test_wrong_dimension_text_never_fuzzy_matches():
    truth = _ir([TextEntity(position=Point(x=50, y=80), text="Ø18H7")])
    predicted = _ir([TextEntity(position=Point(x=50, y=80), text="Ø16H7")])

    result = compare_ir(predicted, truth)

    assert result["exact_sheet"] is False
    assert result["micro"]["matched"] == 0
    assert result["micro"]["false_positive"] == 1
    assert result["micro"]["false_negative"] == 1


def test_small_coordinate_jitter_is_tolerated_but_large_shift_is_not():
    truth = _ir([Circle(center=Point(x=300, y=200), radius=50)])
    close = _ir([Circle(center=Point(x=301, y=200), radius=50.5)])
    far = _ir([Circle(center=Point(x=320, y=200), radius=50)])

    assert compare_ir(close, truth)["exact_sheet"] is True
    assert compare_ir(far, truth)["exact_sheet"] is False


def test_error_details_identify_geometry_near_miss():
    truth = _ir([Circle(id="truth-circle", center=Point(x=300, y=200), radius=50)])
    predicted = _ir([Circle(id="pred-circle", center=Point(x=320, y=200), radius=50)])

    result = compare_ir(predicted, truth, include_details=True)
    details = result["error_details"]["circle"]

    assert details["unmatched_predicted_ids"] == ["pred-circle"]
    assert details["unmatched_truth_ids"] == ["truth-circle"]
    assert details["near_misses"][0]["reason"] == "geometry_out_of_tolerance"


def test_dimension_symbol_variants_and_annotation_canonical_text_match():
    truth = _ir([
        DimensionEntity(
            p1=Point(x=10, y=10),
            p2=Point(x=50, y=10),
            kind="diameter",
            text="Ø40",
            value_mm=40,
        ),
        AnnotationEntity(
            position=Point(x=20, y=20),
            kind="roughness",
            value="3.2",
        ),
    ])
    predicted = _ir([
        DimensionEntity(
            p1=Point(x=10, y=10),
            p2=Point(x=50, y=10),
            kind="diameter",
            text="⌀40",
            value_mm=40,
        ),
        AnnotationEntity(
            position=Point(x=20, y=20),
            kind="roughness",
            value="Ra 3,2",
        ),
    ])

    assert compare_ir(predicted, truth)["exact_sheet"] is True


def test_missing_hatch_hole_is_not_exact():
    boundary = [
        Point(x=10, y=10),
        Point(x=90, y=10),
        Point(x=90, y=90),
    ]
    hole = [
        Point(x=30, y=30),
        Point(x=40, y=30),
        Point(x=40, y=40),
    ]
    truth = _ir([HatchRegion(boundary=boundary, holes=[hole])])
    predicted = _ir([HatchRegion(boundary=boundary)])

    result = compare_ir(predicted, truth)

    assert result["exact_sheet"] is False
    assert result["micro"]["false_positive"] == 1
