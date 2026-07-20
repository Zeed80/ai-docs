"""Compact U-Net for geometric evidence, not direct CAD coordinates."""

from __future__ import annotations

import torch
import torch.nn as nn

from evidence_dataset import EVIDENCE_NAMES


def _block(input_channels: int, output_channels: int) -> nn.Sequential:
    return nn.Sequential(
        nn.Conv2d(input_channels, output_channels, 3, padding=1),
        nn.BatchNorm2d(output_channels),
        nn.ReLU(inplace=True),
        nn.Conv2d(output_channels, output_channels, 3, padding=1),
        nn.BatchNorm2d(output_channels),
        nn.ReLU(inplace=True),
    )


class EvidenceHeatmapModel(nn.Module):
    def __init__(self, base: int = 32):
        super().__init__()
        self.enc1 = _block(1, base)
        self.enc2 = _block(base, base * 2)
        self.enc3 = _block(base * 2, base * 4)
        self.bottleneck = _block(base * 4, base * 8)
        self.pool = nn.MaxPool2d(2)
        self.up3 = nn.ConvTranspose2d(base * 8, base * 4, 2, stride=2)
        self.dec3 = _block(base * 8, base * 4)
        self.up2 = nn.ConvTranspose2d(base * 4, base * 2, 2, stride=2)
        self.dec2 = _block(base * 4, base * 2)
        self.up1 = nn.ConvTranspose2d(base * 2, base, 2, stride=2)
        self.dec1 = _block(base * 2, base)
        self.head = nn.Conv2d(base, len(EVIDENCE_NAMES), 1)

    def forward(self, image: torch.Tensor) -> torch.Tensor:
        enc1 = self.enc1(image)
        enc2 = self.enc2(self.pool(enc1))
        enc3 = self.enc3(self.pool(enc2))
        hidden = self.bottleneck(self.pool(enc3))
        hidden = self.dec3(torch.cat((self.up3(hidden), enc3), dim=1))
        hidden = self.dec2(torch.cat((self.up2(hidden), enc2), dim=1))
        hidden = self.dec1(torch.cat((self.up1(hidden), enc1), dim=1))
        return self.head(hidden)
