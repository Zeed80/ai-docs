"""Learned line-of-interest verifier over dense directional fields."""

from __future__ import annotations

import math

import torch
import torch.nn as nn
import torch.nn.functional as F

PAIR_FEATURES = (
    "endpoint_left",
    "endpoint_right",
    "junction_left",
    "junction_right",
    "line_mean",
    "line_min",
    "line_fraction_03",
    "line_fraction_05",
    "line_fraction_07",
    "direction_mean",
    "direction_min",
    "length",
    "abs_dx",
    "abs_dy",
)


class EdgeVerifier(nn.Module):
    def __init__(self, hidden: int = 64):
        super().__init__()
        self.network = nn.Sequential(
            nn.Linear(len(PAIR_FEATURES), hidden),
            nn.ReLU(inplace=True),
            nn.Dropout(0.1),
            nn.Linear(hidden, hidden),
            nn.ReLU(inplace=True),
            nn.Linear(hidden, 1),
        )

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        return self.network(features).squeeze(-1)


def pair_features(field_output: torch.Tensor, pairs: torch.Tensor) -> torch.Tensor:
    """Return fixed line-profile features for normalized xyxy pairs."""

    if pairs.numel() == 0:
        return torch.empty((0, len(PAIR_FEATURES)), device=field_output.device)
    heatmaps = field_output[:6].sigmoid()
    directions = F.normalize(field_output[6:8], dim=0, eps=1e-6)

    def sample(channels: torch.Tensor, points: torch.Tensor) -> torch.Tensor:
        grid = points.mul(2).sub(1).view(1, -1, 1, 2)
        values = F.grid_sample(
            channels[None],
            grid,
            mode="bilinear",
            padding_mode="border",
            align_corners=True,
        )
        return values[0, :, :, 0].transpose(0, 1)

    left, right = pairs[:, :2], pairs[:, 2:]
    endpoint_left = sample(heatmaps[1:3], left)
    endpoint_right = sample(heatmaps[1:3], right)
    steps = torch.linspace(0, 1, 32, device=pairs.device)
    path = left[:, None, :] * (1 - steps[None, :, None]) + right[:, None, :] * steps[
        None, :, None
    ]
    line = sample(heatmaps[0:1], path.reshape(-1, 2)).reshape(len(pairs), 32)
    direction = sample(directions, path.reshape(-1, 2)).reshape(len(pairs), 32, 2)
    delta = right - left
    theta = torch.atan2(delta[:, 1], delta[:, 0])
    expected = torch.stack((torch.cos(2 * theta), torch.sin(2 * theta)), dim=-1)
    agreement = (direction * expected[:, None, :]).sum(-1)
    length = torch.linalg.vector_norm(delta, dim=-1)
    return torch.stack(
        (
            endpoint_left[:, 0],
            endpoint_right[:, 0],
            endpoint_left[:, 1],
            endpoint_right[:, 1],
            line.mean(-1),
            line.min(-1).values,
            (line >= 0.3).float().mean(-1),
            (line >= 0.5).float().mean(-1),
            (line >= 0.7).float().mean(-1),
            agreement.mean(-1),
            agreement.min(-1).values,
            length,
            delta[:, 0].abs(),
            delta[:, 1].abs(),
        ),
        dim=-1,
    )


def detect_nodes(
    field_output: torch.Tensor,
    *,
    threshold: float = 0.6,
    max_nodes: int = 96,
) -> torch.Tensor:
    """NMS heatmap peaks refined to subpixel weighted centroids."""

    point_map = torch.maximum(field_output[1].sigmoid(), field_output[2].sigmoid())
    maxima = F.max_pool2d(point_map[None, None], 7, stride=1, padding=3)[0, 0]
    ys, xs = torch.where((point_map >= threshold) & (point_map >= maxima - 1e-7))
    if not len(xs):
        return torch.empty((0, 2), device=field_output.device)
    scores = point_map[ys, xs]
    order = scores.argsort(descending=True)
    selected: list[torch.Tensor] = []
    height, width = point_map.shape
    for raw_index in order.tolist():
        x, y = int(xs[raw_index]), int(ys[raw_index])
        if any(torch.linalg.vector_norm(node - torch.tensor((x, y), device=node.device)) < 5 for node in selected):
            continue
        x0, x1 = max(0, x - 2), min(width, x + 3)
        y0, y1 = max(0, y - 2), min(height, y + 3)
        patch = point_map[y0:y1, x0:x1]
        yy, xx = torch.meshgrid(
            torch.arange(y0, y1, device=patch.device),
            torch.arange(x0, x1, device=patch.device),
            indexing="ij",
        )
        weight = patch.clamp(min=1e-6)
        refined = torch.stack(((xx * weight).sum(), (yy * weight).sum())) / weight.sum()
        selected.append(refined)
        if len(selected) >= max_nodes:
            break
    nodes = torch.stack(selected)
    nodes[:, 0] /= max(width - 1, 1)
    nodes[:, 1] /= max(height - 1, 1)
    return nodes


def decode_verified_edges(
    field_output: torch.Tensor,
    verifier: EdgeVerifier,
    *,
    node_threshold: float = 0.6,
    edge_threshold: float = 0.7,
) -> list[dict]:
    nodes = detect_nodes(field_output, threshold=node_threshold)
    if len(nodes) < 2:
        return []
    pair_indices = torch.combinations(torch.arange(len(nodes), device=nodes.device), r=2)
    pairs = torch.cat((nodes[pair_indices[:, 0]], nodes[pair_indices[:, 1]]), dim=-1)
    features = pair_features(field_output, pairs)
    probabilities = verifier(features).sigmoid()
    entities = []
    for index in torch.where(probabilities >= edge_threshold)[0].tolist():
        left, right = pair_indices[index].tolist()
        p1, p2 = nodes[left], nodes[right]
        if math.hypot(float(p2[0] - p1[0]), float(p2[1] - p1[1])) < 0.005:
            continue
        entities.append(
            {
                "type": "segment",
                "line_class": "contour",
                "width_class": "main",
                "confidence": float(probabilities[index]),
                "origin": "neural",
                "assurance": "inferred",
                "p1": {"x": float(p1[0]), "y": float(p1[1])},
                "p2": {"x": float(p2[0]), "y": float(p2[1])},
            }
        )
    return entities
