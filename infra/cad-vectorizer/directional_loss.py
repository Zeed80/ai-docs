"""Sparse heatmap and masked continuous-field objective."""

from __future__ import annotations

import torch
import torch.nn.functional as F

from directional_dataset import HEATMAP_NAMES


def directional_loss(
    output: torch.Tensor,
    target: torch.Tensor,
) -> tuple[torch.Tensor, dict[str, torch.Tensor]]:
    heatmap_logits = output[:, : len(HEATMAP_NAMES)]
    heatmap_target = target[:, : len(HEATMAP_NAMES)]
    positives = heatmap_target.sum(dim=(0, 2, 3))
    total = heatmap_target.shape[0] * heatmap_target.shape[2] * heatmap_target.shape[3]
    positive_weight = ((total - positives) / positives.clamp(min=1)).clamp(max=60)
    bce = F.binary_cross_entropy_with_logits(
        heatmap_logits,
        heatmap_target,
        pos_weight=positive_weight[None, :, None, None],
    )
    probabilities = heatmap_logits.sigmoid()
    intersection = (probabilities * heatmap_target).sum(dim=(0, 2, 3))
    denominator = probabilities.sum(dim=(0, 2, 3)) + heatmap_target.sum(dim=(0, 2, 3))
    dice = 1.0 - ((2 * intersection + 1) / (denominator + 1)).mean()

    direction_mask = heatmap_target[:, 0:1]
    predicted_direction = F.normalize(output[:, 6:8], dim=1, eps=1e-6)
    expected_direction = target[:, 6:8]
    cosine = (predicted_direction * expected_direction).sum(dim=1, keepdim=True)
    direction = ((1.0 - cosine) * direction_mask).sum() / direction_mask.sum().clamp(min=1)

    radius_mask = heatmap_target[:, 5:6]
    predicted_radius = output[:, 8:9].sigmoid()
    radius = (
        F.smooth_l1_loss(predicted_radius, target[:, 8:9], reduction="none")
        * radius_mask
    ).sum() / radius_mask.sum().clamp(min=1)
    total_loss = bce + dice + 0.5 * direction + 2.0 * radius
    return total_loss, {
        "bce": bce.detach(),
        "dice": dice.detach(),
        "direction": direction.detach(),
        "radius": radius.detach(),
    }
