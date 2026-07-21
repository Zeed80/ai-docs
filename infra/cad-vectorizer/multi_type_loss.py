"""Hungarian matching, loss and strict proposal metrics for multi-type v2."""

from __future__ import annotations

import torch
import torch.nn.functional as F
from scipy.optimize import linear_sum_assignment

from multi_type_dataset import TYPE_NAMES

PARAM_MASKS = torch.tensor(
    [
        [0, 0, 0, 0, 0, 0, 0, 0],  # none
        [1, 1, 1, 1, 0, 0, 0, 0],  # segment
        [1, 1, 1, 0, 0, 0, 0, 0],  # circle
        [1, 1, 1, 1, 1, 0, 0, 0],  # arc
        [1, 1, 1, 1, 0, 0, 0, 0],  # text anchor
        [1, 1, 1, 1, 0, 0, 0, 0],  # dimension
        [1, 1, 1, 1, 0, 0, 0, 0],  # annotation + leader
        [1, 1, 1, 1, 0, 0, 0, 0],  # hatch bounds
    ],
    dtype=torch.float32,
)
SUBTYPE_TYPES = {5, 6, 7}


def _parameter_error(
    predicted: torch.Tensor,
    target: torch.Tensor,
    target_types: torch.Tensor,
) -> torch.Tensor:
    """Absolute error with CAD-equivalent endpoint/angle representations.

    Shapes may be ``(Q,T,P)/(1,T,P)/(T,)`` or aligned ``(T,P)/(T,P)/(T,)``.
    """
    direct = (predicted - target).abs()
    angular = target_types == 3
    angular_rows = angular if direct.ndim == 2 else angular.unsqueeze(0)
    angular_slots = torch.tensor(
        [False, False, False, True, True, False, False, False],
        device=direct.device,
    )
    angular_mask = angular_rows.unsqueeze(-1) & angular_slots
    direct = torch.where(
        angular_mask,
        torch.minimum(direct, 1.0 - direct.clamp(max=1.0)),
        direct,
    )
    reversible = (target_types == 1) | (target_types == 5)
    if reversible.any():
        reversed_target = torch.cat(
            (target[..., 2:4], target[..., 0:2], target[..., 4:]), dim=-1
        )
        reverse = (predicted - reversed_target).abs()
        direct_score = direct[..., :4].mean(-1)
        reverse_score = reverse[..., :4].mean(-1)
        choose_reverse = reverse_score < direct_score
        direct = torch.where(choose_reverse.unsqueeze(-1), reverse, direct)
    bounds = target_types == 7
    if bounds.any():
        normalized = torch.cat(
            (
                torch.minimum(predicted[..., 0:1], predicted[..., 2:3]),
                torch.minimum(predicted[..., 1:2], predicted[..., 3:4]),
                torch.maximum(predicted[..., 0:1], predicted[..., 2:3]),
                torch.maximum(predicted[..., 1:2], predicted[..., 3:4]),
                predicted[..., 4:],
            ),
            dim=-1,
        )
        bound_error = (normalized - target).abs()
        direct = torch.where(
            (bounds if direct.ndim == 2 else bounds.unsqueeze(0)).unsqueeze(-1),
            bound_error,
            direct,
        )
    return direct


def assignment(
    type_logits: torch.Tensor,
    params: torch.Tensor,
    target: dict[str, torch.Tensor],
) -> tuple[torch.Tensor, torch.Tensor]:
    if target["types"].numel() == 0:
        empty = torch.empty(0, dtype=torch.long, device=type_logits.device)
        return empty, empty
    probabilities = type_logits.softmax(-1)
    target_types = target["types"].to(type_logits.device)
    target_params = target["params"].to(params.device)
    masks = PARAM_MASKS.to(params.device)[target_types]
    type_cost = -probabilities[:, target_types]
    errors = _parameter_error(
        params[:, None, :], target_params[None, :, :], target_types
    )
    param_cost = (errors * masks[None, :, :]).sum(-1) / masks.sum(-1).clamp(min=1)[None, :]
    rows, columns = linear_sum_assignment(
        (type_cost + 8.0 * param_cost).detach().cpu().numpy()
    )
    return (
        torch.as_tensor(rows, dtype=torch.long, device=type_logits.device),
        torch.as_tensor(columns, dtype=torch.long, device=type_logits.device),
    )


def multi_type_loss(outputs, targets):
    batch_size, n_queries, _ = outputs["type_logits"].shape
    device = outputs["type_logits"].device
    type_targets = torch.zeros(batch_size, n_queries, dtype=torch.long, device=device)
    matched = []
    for batch_index, target in enumerate(targets):
        prediction_indices, target_indices = assignment(
            outputs["type_logits"][batch_index], outputs["params"][batch_index], target
        )
        if prediction_indices.numel():
            type_targets[batch_index, prediction_indices] = target["types"].to(device)[
                target_indices
            ]
            matched.append((batch_index, prediction_indices, target_indices))

    weights = torch.ones(outputs["type_logits"].size(-1), device=device)
    weights[0] = 0.08
    type_loss = F.cross_entropy(
        outputs["type_logits"].flatten(0, 1), type_targets.flatten(), weight=weights
    )
    param_terms = []
    line_terms = []
    width_terms = []
    subtype_terms = []
    for batch_index, prediction_indices, target_indices in matched:
        target = targets[batch_index]
        target_types = target["types"].to(device)[target_indices]
        masks = PARAM_MASKS.to(device)[target_types]
        errors = _parameter_error(
            outputs["params"][batch_index, prediction_indices],
            target["params"].to(device)[target_indices],
            target_types,
        )
        param_terms.append((F.smooth_l1_loss(errors, torch.zeros_like(errors), reduction="none") * masks).sum() / masks.sum().clamp(min=1))
        line_terms.append(
            F.cross_entropy(
                outputs["line_logits"][batch_index, prediction_indices],
                target["line_classes"].to(device)[target_indices],
            )
        )
        width_terms.append(
            F.cross_entropy(
                outputs["width_logits"][batch_index, prediction_indices],
                target["width_classes"].to(device)[target_indices],
            )
        )
        subtype_mask = (target_types == 5) | (target_types == 6) | (target_types == 7)
        if subtype_mask.any():
            subtype_terms.append(
                F.cross_entropy(
                    outputs["subtype_logits"][batch_index, prediction_indices][subtype_mask],
                    target["subtypes"].to(device)[target_indices][subtype_mask],
                )
            )
    zero = outputs["params"].sum() * 0.0
    mean = lambda terms: torch.stack(terms).mean() if terms else zero
    param_loss = mean(param_terms)
    line_loss = mean(line_terms)
    width_loss = mean(width_terms)
    subtype_loss = mean(subtype_terms)
    total = type_loss + 12.0 * param_loss + line_loss + width_loss + subtype_loss
    return total, {
        "type": float(type_loss.detach()),
        "param": float(param_loss.detach()),
        "line": float(line_loss.detach()),
        "width": float(width_loss.detach()),
        "subtype": float(subtype_loss.detach()),
    }


@torch.no_grad()
def proposal_metrics(outputs, targets, *, confidence: float = 0.5, tolerance: float = 0.01):
    totals = {name: {"tp": 0, "pred": 0, "target": 0} for name in TYPE_NAMES[1:]}
    for batch_index, target in enumerate(targets):
        logits = outputs["type_logits"][batch_index]
        probabilities = logits.softmax(-1)
        scores, predicted_types = probabilities.max(-1)
        rows, columns = assignment(logits, outputs["params"][batch_index], target)
        target_types = target["types"].to(logits.device)
        target_params = target["params"].to(logits.device)
        for type_index, name in enumerate(TYPE_NAMES[1:], start=1):
            totals[name]["pred"] += int(
                ((predicted_types == type_index) & (scores >= confidence)).sum()
            )
            totals[name]["target"] += int((target_types == type_index).sum())
        for prediction_index, target_index in zip(rows.tolist(), columns.tolist(), strict=True):
            target_type = int(target_types[target_index])
            if (
                int(predicted_types[prediction_index]) != target_type
                or float(scores[prediction_index]) < confidence
            ):
                continue
            mask = PARAM_MASKS.to(logits.device)[target_type].bool()
            error = _parameter_error(
                outputs["params"][batch_index, prediction_index].unsqueeze(0),
                target_params[target_index].unsqueeze(0),
                target_types[target_index].unsqueeze(0),
            )[0][mask].mean()
            if float(error) <= tolerance:
                totals[TYPE_NAMES[target_type]]["tp"] += 1
    by_type = {}
    for name, count in totals.items():
        precision = count["tp"] / max(count["pred"], 1)
        recall = count["tp"] / max(count["target"], 1)
        by_type[name] = {
            **count,
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / max(precision + recall, 1e-12),
        }
    tp = sum(item["tp"] for item in totals.values())
    predicted = sum(item["pred"] for item in totals.values())
    target_count = sum(item["target"] for item in totals.values())
    precision = tp / max(predicted, 1)
    recall = tp / max(target_count, 1)
    return {
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / max(precision + recall, 1e-12),
        "by_type": by_type,
        "tolerance": tolerance,
    }
