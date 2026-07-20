from __future__ import annotations

import math
import pathlib
import sys

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from directional_dataset import directional_target_from_ir  # noqa: E402
from directional_decode import decode_line_segments  # noqa: E402
from directional_loss import directional_loss  # noqa: E402
from directional_model import DirectionalFieldModel  # noqa: E402


def test_directional_target_contains_endpoints_junction_and_orientation() -> None:
    target = directional_target_from_ir(
        {
            "source": {"image_width": 256, "image_height": 256},
            "entities": [
                {"type": "segment", "p1": {"x": 20, "y": 100}, "p2": {"x": 120, "y": 100}},
                {"type": "segment", "p1": {"x": 120, "y": 100}, "p2": {"x": 120, "y": 180}},
                {"type": "circle", "center": {"x": 190, "y": 80}, "radius": 25},
            ],
        }
    )
    assert target.shape == (9, 256, 256)
    assert float(target[0].sum()) > 0
    assert float(target[1].sum()) > 0
    assert float(target[2].sum()) > 0
    assert float(target[3].sum()) > 0
    assert float(target[5].sum()) > 0
    assert float(target[6, 100, 60]) > 0.9
    assert abs(float(target[7, 100, 60])) < 0.1
    assert float(target[8].max()) > 0


def test_directional_model_and_loss_are_finite() -> None:
    model = DirectionalFieldModel(base=8)
    output = model(torch.rand(1, 1, 256, 256))
    target = torch.zeros_like(output)
    target[:, 0, 20, 10:80] = 1
    target[:, 6, 20, 10:80] = 1
    loss, parts = directional_loss(output, target)
    assert output.shape == (1, 9, 256, 256)
    assert torch.isfinite(loss)
    assert all(torch.isfinite(value) for value in parts.values())


def test_decoder_requires_endpoints_line_support_and_direction() -> None:
    output = torch.full((9, 64, 64), -10.0)
    output[0, 30, 8:57] = 10
    for x in (8, 56):
        output[1, 30, x] = 10
    output[6, 30, 8:57] = 1
    output[7, 30, 8:57] = 0
    segments = decode_line_segments(output)
    assert len(segments) == 1
    assert math.isclose(segments[0]["p1"]["y"], 30)
    assert math.isclose(segments[0]["p2"]["y"], 30)

    output[6, 30, 8:57] = -1  # cos(2 theta) now says perpendicular
    assert decode_line_segments(output) == []
