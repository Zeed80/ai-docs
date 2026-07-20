from __future__ import annotations

import pathlib
import sys

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from graph_dataset import graph_target_from_ir  # noqa: E402
from graph_decode import decode_graph_segments  # noqa: E402
from graph_loss import cad_graph_loss  # noqa: E402
from graph_model import CadGraphModel  # noqa: E402


def test_graph_target_merges_vertices_and_marks_junctions() -> None:
    target = graph_target_from_ir(
        {
            "source": {"image_width": 100, "image_height": 100},
            "entities": [
                {"type": "segment", "p1": {"x": 10, "y": 20}, "p2": {"x": 50, "y": 20}},
                {"type": "segment", "p1": {"x": 50, "y": 20}, "p2": {"x": 50, "y": 80}},
            ],
        }
    )
    assert target["coords"].shape == (3, 2)
    assert sorted(target["types"].tolist()) == [1, 1, 2]
    assert int(target["adjacency"].sum()) == 4


def test_graph_model_loss_and_decoder_contract() -> None:
    model = CadGraphModel(d_model=32, n_queries=4, n_layers=1, n_heads=4, dim_ff=64)
    outputs = model(torch.rand(1, 1, 256, 256))
    target = {
        "coords": torch.tensor([[0.1, 0.2], [0.8, 0.2]]),
        "types": torch.tensor([1, 1]),
        "adjacency": torch.tensor([[0.0, 1.0], [1.0, 0.0]]),
    }
    loss, parts = cad_graph_loss(outputs, [target])
    assert torch.isfinite(loss)
    assert parts["matched"] == 2

    synthetic = {
        "type_logits": torch.tensor([[[0.0, 8.0, 0.0], [0.0, 8.0, 0.0]]]),
        "coords": torch.tensor([[[0.1, 0.2], [0.8, 0.2]]]),
        "edge_logits": torch.tensor([[[-20.0, 8.0], [8.0, -20.0]]]),
    }
    entities = decode_graph_segments(synthetic)
    assert len(entities) == 1
    assert entities[0]["p1"] == {"x": 0.10000000149011612, "y": 0.20000000298023224}
