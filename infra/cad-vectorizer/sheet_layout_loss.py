"""Hungarian box/class objective for global sheet layout."""

from __future__ import annotations

import torch
import torch.nn.functional as F
from scipy.optimize import linear_sum_assignment


def _xyxy(boxes: torch.Tensor) -> torch.Tensor:
    center, size = boxes[..., :2], boxes[..., 2:]
    return torch.cat((center - size / 2, center + size / 2), dim=-1)


def pairwise_iou(left: torch.Tensor, right: torch.Tensor) -> torch.Tensor:
    left, right = _xyxy(left), _xyxy(right)
    lo = torch.maximum(left[:, None, :2], right[None, :, :2])
    hi = torch.minimum(left[:, None, 2:], right[None, :, 2:])
    intersection = (hi - lo).clamp(min=0).prod(-1)
    left_area = (left[:, 2:] - left[:, :2]).clamp(min=0).prod(-1)
    right_area = (right[:, 2:] - right[:, :2]).clamp(min=0).prod(-1)
    union = left_area[:, None] + right_area[None, :] - intersection
    return intersection / union.clamp(min=1e-8)


def _assignment(type_logits, boxes, target):
    if target["types"].numel() == 0:
        empty = torch.empty(0, dtype=torch.long, device=boxes.device)
        return empty, empty
    target_types = target["types"].to(boxes.device)
    target_boxes = target["boxes"].to(boxes.device)
    class_cost = -type_logits.softmax(-1)[:, target_types]
    box_cost = torch.cdist(boxes, target_boxes, p=1)
    iou_cost = 1.0 - pairwise_iou(boxes, target_boxes)
    rows, columns = linear_sum_assignment(
        (class_cost + 5.0 * box_cost + 2.0 * iou_cost).detach().cpu().numpy()
    )
    return (
        torch.as_tensor(rows, dtype=torch.long, device=boxes.device),
        torch.as_tensor(columns, dtype=torch.long, device=boxes.device),
    )


def sheet_layout_loss(outputs, targets):
    batch_size, n_queries, _ = outputs["type_logits"].shape
    type_targets = torch.zeros(
        batch_size, n_queries, dtype=torch.long, device=outputs["type_logits"].device
    )
    matched = []
    for batch_index, target in enumerate(targets):
        prediction_indices, target_indices = _assignment(
            outputs["type_logits"][batch_index],
            outputs["boxes"][batch_index],
            target,
        )
        if prediction_indices.numel():
            type_targets[batch_index, prediction_indices] = target["types"].to(
                type_targets.device
            )[target_indices]
            matched.append((batch_index, prediction_indices, target_indices))
    weights = torch.ones(outputs["type_logits"].size(-1), device=type_targets.device)
    weights[0] = 0.15
    class_loss = F.cross_entropy(
        outputs["type_logits"].reshape(-1, outputs["type_logits"].size(-1)),
        type_targets.reshape(-1),
        weight=weights,
    )
    box_terms, ious, correct = [], [], 0
    for batch_index, prediction_indices, target_indices in matched:
        target = targets[batch_index]
        predicted = outputs["boxes"][batch_index, prediction_indices]
        expected = target["boxes"].to(predicted.device)[target_indices]
        box_terms.append(F.smooth_l1_loss(predicted, expected))
        ious.extend(pairwise_iou(predicted, expected).diag())
        kinds = outputs["type_logits"][batch_index, prediction_indices].argmax(-1)
        correct += int(
            (kinds == target["types"].to(kinds.device)[target_indices]).sum()
        )
    zero = outputs["boxes"].sum() * 0
    box_loss = torch.stack(box_terms).mean() if box_terms else zero
    mean_iou = float(torch.stack(ious).mean().detach()) if ious else 0.0
    matched_count = sum(len(item[1]) for item in matched)
    total = class_loss + 15.0 * box_loss
    return total, {
        "class": float(class_loss.detach()),
        "box": float(box_loss.detach()),
        "mean_iou": mean_iou,
        "class_accuracy": correct / max(matched_count, 1),
        "matched": float(matched_count),
    }
