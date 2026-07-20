"""Hungarian node matching plus topology-supervised edge loss."""

from __future__ import annotations

import torch
import torch.nn.functional as F
from scipy.optimize import linear_sum_assignment


def graph_assignment(
    type_logits: torch.Tensor,
    coords: torch.Tensor,
    target: dict[str, torch.Tensor],
) -> tuple[torch.Tensor, torch.Tensor]:
    count = target["types"].numel()
    if count == 0:
        empty = torch.empty(0, dtype=torch.long, device=type_logits.device)
        return empty, empty
    probabilities = type_logits.softmax(-1)
    target_types = target["types"].to(type_logits.device)
    target_coords = target["coords"].to(coords.device)
    type_cost = -probabilities[:, target_types]
    coord_cost = torch.cdist(coords, target_coords, p=1)
    cost = type_cost + 8.0 * coord_cost
    rows, columns = linear_sum_assignment(cost.detach().cpu().numpy())
    return (
        torch.as_tensor(rows, dtype=torch.long, device=type_logits.device),
        torch.as_tensor(columns, dtype=torch.long, device=type_logits.device),
    )


def cad_graph_loss(
    outputs: dict[str, torch.Tensor],
    targets: list[dict[str, torch.Tensor]],
) -> tuple[torch.Tensor, dict[str, float]]:
    batch_size, n_queries, _ = outputs["type_logits"].shape
    device = outputs["type_logits"].device
    type_targets = torch.zeros(batch_size, n_queries, dtype=torch.long, device=device)
    matched = []
    for batch_index, target in enumerate(targets):
        prediction_indices, target_indices = graph_assignment(
            outputs["type_logits"][batch_index],
            outputs["coords"][batch_index],
            target,
        )
        if prediction_indices.numel():
            type_targets[batch_index, prediction_indices] = target["types"].to(device)[
                target_indices
            ]
        matched.append((batch_index, prediction_indices, target_indices))

    weights = torch.ones(outputs["type_logits"].size(-1), device=device)
    weights[0] = 0.15
    type_loss = F.cross_entropy(
        outputs["type_logits"].reshape(-1, outputs["type_logits"].size(-1)),
        type_targets.reshape(-1),
        weight=weights,
    )
    coord_terms = []
    edge_terms = []
    degree_terms = []
    for batch_index, prediction_indices, target_indices in matched:
        if prediction_indices.numel() == 0:
            continue
        target = targets[batch_index]
        expected_coords = target["coords"].to(device)[target_indices]
        coord_terms.append(
            F.l1_loss(
                outputs["coords"][batch_index, prediction_indices],
                expected_coords,
            )
        )
        expected_edges = target["adjacency"].to(device)[
            target_indices[:, None], target_indices[None, :]
        ]
        predicted_edges = outputs["edge_logits"][batch_index][
            prediction_indices[:, None], prediction_indices[None, :]
        ]
        upper = torch.triu(
            torch.ones_like(expected_edges, dtype=torch.bool),
            diagonal=1,
        )
        expected_upper = expected_edges[upper]
        predicted_upper = predicted_edges[upper]
        positives = expected_upper.sum()
        negatives = expected_upper.numel() - positives
        positive_weight = (negatives / positives.clamp(min=1)).clamp(max=30)
        edge_terms.append(
            F.binary_cross_entropy_with_logits(
                predicted_upper,
                expected_upper,
                pos_weight=positive_weight,
            )
        )
        predicted_degree = predicted_edges.sigmoid().sum(dim=-1)
        expected_degree = expected_edges.sum(dim=-1)
        degree_terms.append(F.smooth_l1_loss(predicted_degree, expected_degree))
    zero = outputs["coords"].sum() * 0
    coord_loss = torch.stack(coord_terms).mean() if coord_terms else zero
    edge_loss = torch.stack(edge_terms).mean() if edge_terms else zero
    degree_loss = torch.stack(degree_terms).mean() if degree_terms else zero
    # Entity matching allows only 0.0025 normalized endpoint error. SmoothL1
    # makes the gradient vanish precisely in this subpixel regime; direct L1
    # must remain a first-class objective instead of being hidden by topology.
    total = type_loss + 15.0 * coord_loss + 2.0 * edge_loss + 0.1 * degree_loss
    return total, {
        "type": float(type_loss.detach()),
        "coord": float(coord_loss.detach()),
        "edge": float(edge_loss.detach()),
        "degree": float(degree_loss.detach()),
        "matched": float(sum(len(item[1]) for item in matched)),
    }
