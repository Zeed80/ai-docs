from __future__ import annotations

import pathlib
import sys

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from evidence_dataset import evidence_target_from_ir  # noqa: E402
from evidence_loss import evidence_loss  # noqa: E402
from evidence_model import EvidenceHeatmapModel  # noqa: E402


def test_evidence_target_keeps_geometry_types_separate() -> None:
    target = evidence_target_from_ir(
        {
            "source": {"image_width": 256, "image_height": 256},
            "entities": [
                {"type": "segment", "p1": {"x": 10, "y": 20}, "p2": {"x": 200, "y": 20}},
                {"type": "circle", "center": {"x": 80, "y": 100}, "radius": 20},
                {
                    "type": "arc",
                    "center": {"x": 180, "y": 100},
                    "radius": 20,
                    "start_angle": 0,
                    "end_angle": 180,
                },
            ],
        }
    )
    assert target.shape == (3, 256, 256)
    assert all(float(target[index].sum()) > 0 for index in range(3))


def test_evidence_model_preserves_raster_resolution() -> None:
    model = EvidenceHeatmapModel(base=8)
    output = model(torch.rand(2, 1, 256, 256))
    assert output.shape == (2, 3, 256, 256)


def test_evidence_loss_is_finite_for_sparse_and_empty_channels() -> None:
    target = torch.zeros(1, 3, 32, 32)
    target[:, 0, 10, 4:20] = 1
    loss = evidence_loss(torch.zeros_like(target), target)
    assert torch.isfinite(loss)
