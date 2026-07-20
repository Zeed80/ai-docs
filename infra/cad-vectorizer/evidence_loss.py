"""Class-balanced BCE + Dice objective for sparse geometry evidence."""

from __future__ import annotations

import torch
import torch.nn.functional as F


def evidence_loss(logits: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
    positives = targets.sum(dim=(0, 2, 3))
    total = targets.shape[0] * targets.shape[2] * targets.shape[3]
    positive_weight = ((total - positives) / positives.clamp(min=1)).clamp(max=40)
    bce = F.binary_cross_entropy_with_logits(
        logits,
        targets,
        pos_weight=positive_weight[None, :, None, None],
    )
    probabilities = logits.sigmoid()
    intersection = (probabilities * targets).sum(dim=(0, 2, 3))
    denominator = probabilities.sum(dim=(0, 2, 3)) + targets.sum(dim=(0, 2, 3))
    dice = 1.0 - ((2 * intersection + 1) / (denominator + 1)).mean()
    return bce + dice
