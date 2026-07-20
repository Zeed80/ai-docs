"""Bipartite matching and loss for unordered primitive predictions."""

from __future__ import annotations

import torch
import torch.nn.functional as F
from scipy.optimize import linear_sum_assignment

PARAM_MASKS = torch.tensor(
    [
        [0, 0, 0, 0, 0],  # none
        [1, 1, 1, 1, 0],  # segment
        [1, 1, 1, 0, 0],  # circle
        [1, 1, 1, 1, 1],  # arc
    ],
    dtype=torch.float32,
)


def _assignment(
    type_logits: torch.Tensor,
    params: torch.Tensor,
    target: dict[str, torch.Tensor],
) -> tuple[torch.Tensor, torch.Tensor]:
    count = target["types"].numel()
    if count == 0:
        empty = torch.empty(0, dtype=torch.long, device=type_logits.device)
        return empty, empty
    probabilities = type_logits.softmax(-1)
    target_types = target["types"].to(type_logits.device)
    target_params = target["params"].to(params.device)
    type_cost = -probabilities[:, target_types]
    masks = PARAM_MASKS.to(params.device)[target_types]
    param_cost = (
        (params[:, None, :] - target_params[None, :, :]).abs() * masks[None, :, :]
    ).sum(-1) / masks.sum(-1).clamp(min=1)[None, :]
    cost = type_cost + 5.0 * param_cost
    rows, columns = linear_sum_assignment(cost.detach().cpu().numpy())
    return (
        torch.as_tensor(rows, dtype=torch.long, device=type_logits.device),
        torch.as_tensor(columns, dtype=torch.long, device=type_logits.device),
    )


def primitive_set_loss(
    outputs: dict[str, torch.Tensor],
    targets: list[dict[str, torch.Tensor]],
) -> tuple[torch.Tensor, dict[str, float]]:
    batch_size, n_queries, _ = outputs["type_logits"].shape
    type_targets = torch.zeros(
        batch_size, n_queries, dtype=torch.long, device=outputs["type_logits"].device
    )
    matched: list[tuple[int, torch.Tensor, torch.Tensor]] = []
    for batch_index, target in enumerate(targets):
        prediction_indices, target_indices = _assignment(
            outputs["type_logits"][batch_index],
            outputs["params"][batch_index],
            target,
        )
        if prediction_indices.numel():
            type_targets[batch_index, prediction_indices] = target["types"].to(
                type_targets.device
            )[target_indices]
            matched.append((batch_index, prediction_indices, target_indices))

    class_weights = torch.ones(
        outputs["type_logits"].size(-1), device=outputs["type_logits"].device
    )
    class_weights[0] = 0.1
    type_loss = F.cross_entropy(
        outputs["type_logits"].reshape(-1, outputs["type_logits"].size(-1)),
        type_targets.reshape(-1),
        weight=class_weights,
    )

    param_terms = []
    line_terms = []
    width_terms = []
    for batch_index, prediction_indices, target_indices in matched:
        target = targets[batch_index]
        target_types = target["types"].to(type_targets.device)[target_indices]
        masks = PARAM_MASKS.to(type_targets.device)[target_types]
        predicted_params = outputs["params"][batch_index, prediction_indices]
        target_params = target["params"].to(type_targets.device)[target_indices]
        param_terms.append(
            (
                F.smooth_l1_loss(predicted_params, target_params, reduction="none")
                * masks
            ).sum()
            / masks.sum().clamp(min=1)
        )
        line_terms.append(
            F.cross_entropy(
                outputs["line_logits"][batch_index, prediction_indices],
                target["line_classes"].to(type_targets.device)[target_indices],
            )
        )
        width_terms.append(
            F.cross_entropy(
                outputs["width_logits"][batch_index, prediction_indices],
                target["width_classes"].to(type_targets.device)[target_indices],
            )
        )
    zero = outputs["params"].sum() * 0.0
    param_loss = torch.stack(param_terms).mean() if param_terms else zero
    line_loss = torch.stack(line_terms).mean() if line_terms else zero
    width_loss = torch.stack(width_terms).mean() if width_terms else zero
    total = type_loss + 10.0 * param_loss + line_loss + width_loss
    return total, {
        "type": float(type_loss.detach()),
        "param": float(param_loss.detach()),
        "line": float(line_loss.detach()),
        "width": float(width_loss.detach()),
        "matched": float(sum(len(item[1]) for item in matched)),
    }
