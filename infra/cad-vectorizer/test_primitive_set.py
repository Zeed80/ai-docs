from __future__ import annotations

import pathlib
import sys

import pytest
import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from primitive_dataset import targets_from_ir_dict  # noqa: E402
from primitive_loss import primitive_set_loss  # noqa: E402
from primitive_model import PrimitiveSetModel  # noqa: E402


def test_target_converter_expands_closed_polyline_without_order_contract() -> None:
    ir = {
        "source": {"image_width": 200, "image_height": 100},
        "entities": [
            {
                "type": "circle",
                "center": {"x": 100, "y": 50},
                "radius": 20,
            },
            {
                "type": "polyline",
                "closed": True,
                "points": [
                    {"x": 0, "y": 0},
                    {"x": 100, "y": 0},
                    {"x": 100, "y": 100},
                ],
            },
        ],
    }

    target = targets_from_ir_dict(ir)

    assert target["types"].tolist() == [2, 1, 1, 1]
    assert target["params"][0].tolist()[:3] == pytest.approx([0.5, 0.5, 0.1])


def test_model_outputs_fixed_unordered_query_set() -> None:
    model = PrimitiveSetModel(
        d_model=32, n_queries=12, n_layers=1, n_heads=2, dim_ff=64
    )
    output = model(torch.rand(2, 1, 256, 256))

    assert output["type_logits"].shape == (2, 12, 4)
    assert output["params"].shape == (2, 12, 5)
    assert torch.all((output["params"] >= 0) & (output["params"] <= 1))


def test_hungarian_loss_matches_targets_independent_of_query_order() -> None:
    target = {
        "types": torch.tensor([1, 2]),
        "params": torch.tensor(
            [[0.1, 0.2, 0.8, 0.2, 0.0], [0.5, 0.5, 0.1, 0.0, 0.0]]
        ),
        "line_classes": torch.tensor([0, 0]),
        "width_classes": torch.tensor([0, 0]),
    }
    type_logits = torch.full((1, 3, 4), -8.0)
    type_logits[0, 0, 2] = 8.0  # circle comes first
    type_logits[0, 1, 1] = 8.0  # segment comes second
    type_logits[0, 2, 0] = 8.0
    output = {
        "type_logits": type_logits,
        "params": torch.tensor(
            [[[0.5, 0.5, 0.1, 0.0, 0.0], [0.1, 0.2, 0.8, 0.2, 0.0], [0] * 5]]
        ),
        "line_logits": torch.zeros(1, 3, 6),
        "width_logits": torch.zeros(1, 3, 2),
    }

    loss, parts = primitive_set_loss(output, [target])

    assert torch.isfinite(loss)
    assert parts["matched"] == 2
    assert parts["param"] == 0.0
