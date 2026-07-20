from __future__ import annotations

import pathlib
import sys

import pytest
import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sheet_layout_dataset import target_from_row  # noqa: E402
from sheet_layout_loss import pairwise_iou, sheet_layout_loss  # noqa: E402
from sheet_layout_model import SheetLayoutModel  # noqa: E402


def test_layout_target_uses_normalized_center_size() -> None:
    target = target_from_row(
        {
            "width": 1000,
            "height": 500,
            "targets": [{"kind": "view", "box": [100, 50, 500, 250]}],
        }
    )
    assert target["types"].tolist() == [1]
    assert target["boxes"][0].tolist() == pytest.approx([0.3, 0.3, 0.4, 0.4])


def test_layout_model_has_variable_view_query_contract() -> None:
    model = SheetLayoutModel(
        d_model=32, n_queries=5, n_layers=1, n_heads=2, dim_ff=64
    )
    output = model(torch.rand(2, 1, 256, 256))
    assert output["type_logits"].shape == (2, 5, 2)
    assert output["boxes"].shape == (2, 5, 4)
    assert torch.all((output["boxes"] >= 0) & (output["boxes"] <= 1))


def test_layout_loss_matches_views_independent_of_query_order() -> None:
    output = {
        "type_logits": torch.tensor(
            [[[0.0, 8.0], [0.0, 8.0], [8.0, 0.0]]]
        ),
        "boxes": torch.tensor(
            [[[0.7, 0.5, 0.2, 0.4], [0.2, 0.5, 0.2, 0.4], [0.0] * 4]]
        ),
    }
    targets = [
        {
            "types": torch.tensor([1, 1]),
            "boxes": torch.tensor([[0.2, 0.5, 0.2, 0.4], [0.7, 0.5, 0.2, 0.4]]),
        }
    ]
    loss, parts = sheet_layout_loss(output, targets)
    assert torch.isfinite(loss)
    assert parts["matched"] == 2
    assert parts["mean_iou"] == pytest.approx(1.0)
    assert parts["class_accuracy"] == 1.0
    assert pairwise_iou(targets[0]["boxes"], targets[0]["boxes"]).diag().tolist() == [
        1.0,
        1.0,
    ]
