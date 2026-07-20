"""U-Net producing geometry heatmaps plus orientation/radius fields."""

from __future__ import annotations

import torch
import torch.nn as nn

from directional_dataset import N_OUTPUT_CHANNELS
from evidence_model import EvidenceHeatmapModel


class DirectionalFieldModel(EvidenceHeatmapModel):
    def __init__(self, base: int = 32):
        super().__init__(base=base)
        self.head = nn.Conv2d(base, N_OUTPUT_CHANNELS, 1)

    def split(self, output: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
        return output[:, :6], output[:, 6:]
