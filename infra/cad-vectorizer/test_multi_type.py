from __future__ import annotations

import torch

from multi_type_dataset import SUBTYPE_INDEX, TYPE_INDEX, targets_from_ir_dict
from multi_type_loss import multi_type_loss, proposal_metrics
from multi_type_model import MultiTypeProposalModel


def _ir():
    entity = {"line_class": "contour", "width_class": "main"}
    return {"source": {"image_width": 100, "image_height": 200}, "entities": [
        {**entity, "type": "segment", "p1": {"x": 10, "y": 20}, "p2": {"x": 30, "y": 40}},
        {**entity, "type": "circle", "center": {"x": 50, "y": 60}, "radius": 20},
        {**entity, "type": "arc", "center": {"x": 20, "y": 40}, "radius": 10, "start_angle": 0, "end_angle": 180},
        {**entity, "type": "text", "position": {"x": 10, "y": 10}, "text": "M20", "height": 4},
        {**entity, "type": "dimension", "kind": "diameter", "p1": {"x": 1, "y": 2}, "p2": {"x": 3, "y": 4}},
        {**entity, "type": "annotation", "kind": "datum", "position": {"x": 6, "y": 8}, "leader": {"x": 9, "y": 10}},
        {**entity, "type": "hatch", "pattern": "solid", "boundary": [{"x": 1, "y": 2}, {"x": 9, "y": 2}, {"x": 9, "y": 12}]},
    ]}


def test_target_contract_covers_all_proposal_types():
    target = targets_from_ir_dict(_ir())
    assert target["types"].tolist() == list(range(1, 8))
    assert target["subtypes"][4].item() == SUBTYPE_INDEX["diameter"]
    assert target["subtypes"][5].item() == SUBTYPE_INDEX["datum"]
    assert target["subtypes"][6].item() == SUBTYPE_INDEX["solid"]
    assert target["params"].shape == (7, 8)


def test_model_loss_and_strict_metrics_contract():
    model = MultiTypeProposalModel(d_model=32, n_queries=10, n_layers=1, n_heads=4, dim_ff=64)
    outputs = model(torch.rand(1, 1, 256, 256))
    target = targets_from_ir_dict(_ir())
    loss, parts = multi_type_loss(outputs, [target])
    assert torch.isfinite(loss)
    loss.backward()
    assert set(parts) == {"type", "param", "line", "width", "subtype"}
    assert outputs["params"].shape == (1, 10, 8)

    exact = {
        "type_logits": torch.full((1, 7, 8), -20.0),
        "params": target["params"].unsqueeze(0).clone(),
        "line_logits": torch.zeros(1, 7, 6),
        "width_logits": torch.zeros(1, 7, 2),
        "subtype_logits": torch.zeros(1, 7, 12),
    }
    exact["type_logits"][0, torch.arange(7), target["types"]] = 20.0
    metrics = proposal_metrics(exact, [target])
    assert metrics["f1"] == 1.0
    assert metrics["by_type"]["text"]["tp"] == 1
    assert TYPE_INDEX["hatch"] == 7

    reversed_segment = {key: value.clone() for key, value in exact.items()}
    reversed_segment["params"][0, 0, :4] = exact["params"][0, 0, [2, 3, 0, 1]]
    assert proposal_metrics(reversed_segment, [target])["f1"] == 1.0
