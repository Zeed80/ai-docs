from __future__ import annotations

import pathlib
import os
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
os.environ["REPO_BACKEND"] = str(ROOT / "backend")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from serve import (  # noqa: E402
    _directional_output_to_entities,
    _verified_edge_output_to_entities,
    _layout_outputs_to_regions,
    _multi_type_outputs_to_entities,
    _primitive_outputs_to_entities,
    _rows_to_entities,
)


def test_normalized_coordinates_scale_to_original_image_size() -> None:
    # SEG command, five model params, line class, width class.
    rows = [(1, [0.25, 0.5, 0.75, 1.0, -1.0], 0, 0)]

    entities = _rows_to_entities(rows, image_width=1600, image_height=1000)

    assert len(entities) == 1
    assert entities[0].p1 == {"x": 400.0, "y": 500.0}
    assert entities[0].p2 == {"x": 1200.0, "y": 1000.0}


def test_unconstrained_regression_cannot_escape_image() -> None:
    rows = [(3, [-0.5, 1.5, 2.0, -1.0, -1.0], 0, 0)]

    entities = _rows_to_entities(rows, image_width=640, image_height=480)

    assert entities[0].center == {"x": 0.0, "y": 480.0}
    assert entities[0].radius == 640.0


def test_primitive_set_outputs_preserve_confidence_and_image_scale() -> None:
    import torch

    outputs = {
        "type_logits": torch.tensor(
            [[[0.0, 8.0, 0.0, 0.0], [8.0, 0.0, 0.0, 0.0]]]
        ),
        "params": torch.tensor(
            [[[0.25, 0.5, 0.75, 0.5, 0.0], [0.0, 0.0, 0.0, 0.0, 0.0]]]
        ),
        "line_logits": torch.tensor(
            [
                [
                    [8.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    [8.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                ]
            ]
        ),
        "width_logits": torch.tensor([[[8.0, 0.0], [8.0, 0.0]]]),
    }

    entities = _primitive_outputs_to_entities(outputs, 1600, 1000)

    assert len(entities) == 1
    assert entities[0].type == "segment"
    assert entities[0].p1 == {"x": 400.0, "y": 500.0}
    assert entities[0].p2 == {"x": 1200.0, "y": 500.0}
    assert entities[0].confidence > 0.99


def test_multi_type_outputs_are_valid_inferred_cad_ir_proposals() -> None:
    import torch
    from pydantic import TypeAdapter

    from app.ai.cad_ir.schema import Entity

    outputs = {
        "type_logits": torch.full((1, 7, 8), -8.0),
        "params": torch.tensor([[
            [0.1, 0.2, 0.3, 0.4, 0, 0, 0, 0],
            [0.5, 0.5, 0.1, 0, 0, 0, 0, 0],
            [0.5, 0.5, 0.1, 0.25, 0.75, 0, 0, 0],
            [0.2, 0.3, 0.02, 0.5, 0, 0, 0, 0],
            [0.1, 0.2, 0.8, 0.2, 0, 0, 0, 0],
            [0.3, 0.4, 0.7, 0.8, 0, 0, 0, 0],
            [0.8, 0.8, 0.2, 0.2, 0, 0, 0, 0],
        ]]),
        "line_logits": torch.zeros(1, 7, 6),
        "width_logits": torch.zeros(1, 7, 2),
        "subtype_logits": torch.zeros(1, 7, 12),
    }
    for index in range(7):
        outputs["type_logits"][0, index, index + 1] = 8.0
    outputs["subtype_logits"][0, 4, 2] = 8.0  # diameter
    outputs["subtype_logits"][0, 5, 8] = 8.0  # datum
    outputs["subtype_logits"][0, 6, 11] = 8.0  # solid

    entities = _multi_type_outputs_to_entities(outputs, 1000, 500)
    assert [entity.type for entity in entities] == [
        "segment", "circle", "arc", "text", "dimension", "annotation", "hatch"
    ]
    adapter = TypeAdapter(Entity)
    parsed = [adapter.validate_python(entity.model_dump()) for entity in entities]
    assert all(entity.assurance == "inferred" for entity in parsed)
    assert parsed[4].kind == "diameter"
    assert parsed[5].kind == "datum"
    assert parsed[6].pattern == "solid"


def test_layout_regions_scale_to_sheet_and_suppress_duplicate_queries() -> None:
    import torch

    outputs = {
        "type_logits": torch.tensor(
            [[[0.0, 8.0], [0.0, 7.0], [8.0, 0.0]]]
        ),
        "boxes": torch.tensor(
            [[[0.5, 0.5, 0.4, 0.6], [0.51, 0.5, 0.4, 0.6], [0.0] * 4]]
        ),
    }
    regions = _layout_outputs_to_regions(outputs, 1000, 500)
    assert len(regions) == 1
    assert regions[0][:4] == (300, 100, 700, 400)


def test_directional_output_scales_proposal_to_original_tile() -> None:
    import torch

    output = torch.full((9, 256, 256), -10.0)
    output[6:] = 0
    output[0, 100, 20:221] = 10
    output[1, 100, 20] = 10
    output[1, 100, 220] = 10
    output[6, 100, 20:221] = 1
    entities = _directional_output_to_entities(output, 1280, 640)
    assert len(entities) == 1
    assert entities[0].p1["y"] == 250
    assert entities[0].p2["y"] == 250
    assert {entities[0].p1["x"], entities[0].p2["x"]} == {100, 1100}


def test_verified_edge_output_scales_normalized_graph(monkeypatch) -> None:
    import serve
    import torch

    monkeypatch.setattr(
        serve,
        "decode_verified_edges",
        lambda *args, **kwargs: [
            {
                "type": "segment",
                "line_class": "contour",
                "width_class": "main",
                "confidence": 0.9,
                "origin": "neural",
                "assurance": "inferred",
                "p1": {"x": 0.25, "y": 0.5},
                "p2": {"x": 0.75, "y": 0.5},
            }
        ],
    )
    entities = _verified_edge_output_to_entities(
        torch.zeros(9, 256, 256),
        object(),
        1000,
        600,
        node_threshold=0.7,
        edge_threshold=0.5,
    )
    assert entities[0].p1 == {"x": 250.0, "y": 300.0}
    assert entities[0].p2 == {"x": 750.0, "y": 300.0}
