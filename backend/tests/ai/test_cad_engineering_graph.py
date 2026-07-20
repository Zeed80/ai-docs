from __future__ import annotations

from app.ai.cad_engineering_graph import build_engineering_graph
from app.ai.cad_ir.schema import (
    CadIR,
    Circle,
    DimensionEntity,
    Point,
    Segment,
    SourceInfo,
)


def test_graph_finds_repeated_holes_and_never_calls_inference_exact() -> None:
    entities = [
        Segment(p1=Point(x=20, y=20), p2=Point(x=180, y=20)),
        Segment(p1=Point(x=180, y=20), p2=Point(x=180, y=180)),
        Segment(p1=Point(x=180, y=180), p2=Point(x=20, y=180)),
        Segment(p1=Point(x=20, y=180), p2=Point(x=20, y=20)),
        Circle(center=Point(x=60, y=60), radius=10),
        Circle(center=Point(x=140, y=60), radius=10),
        Circle(center=Point(x=60, y=140), radius=10),
        Circle(center=Point(x=140, y=140), radius=10),
    ]
    ir = CadIR(
        source=SourceInfo(image_width=400, image_height=300, kind="import"),
        entities=entities,
    )

    graph = build_engineering_graph(ir, profile="mechanical")

    assert graph.views
    pattern = next(feature for feature in graph.features if feature.kind == "hole_pattern")
    assert pattern.parameters["count"] == 4
    assert pattern.status == "inferred"
    assert graph.exact_ready is False


def test_dimension_relation_keeps_evidence_and_value() -> None:
    circle = Circle(center=Point(x=100, y=100), radius=20)
    dimension = DimensionEntity(
        kind="diameter",
        p1=Point(x=80, y=100),
        p2=Point(x=120, y=100),
        value_mm=40,
        text="Ø40",
    )
    ir = CadIR(
        source=SourceInfo(image_width=300, image_height=200, kind="import"),
        entities=[circle, dimension],
    )

    graph = build_engineering_graph(ir, profile="mechanical")

    assert graph.dimensions[0].relation == "diameter"
    assert graph.dimensions[0].target_entity_ids == [circle.id]
    assert graph.dimensions[0].value_mm == 40
    assert graph.dimensions[0].evidence
