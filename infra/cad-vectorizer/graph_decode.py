"""Decode node queries and adjacency logits into inferred CAD segments."""

from __future__ import annotations

import math

import torch


def decode_graph_segments(
    outputs: dict[str, torch.Tensor],
    batch_index: int = 0,
    *,
    node_threshold: float = 0.5,
    edge_threshold: float = 0.5,
    min_length: float = 0.005,
) -> list[dict]:
    probabilities = outputs["type_logits"][batch_index].softmax(-1)
    scores, types = probabilities[:, 1:].max(-1)
    types = types + 1
    active = torch.where(scores >= node_threshold)[0]
    coords = outputs["coords"][batch_index]
    edge_probabilities = outputs["edge_logits"][batch_index].sigmoid()
    entities = []
    for offset, left in enumerate(active.tolist()):
        for right in active[offset + 1 :].tolist():
            edge_score = float(edge_probabilities[left, right])
            if edge_score < edge_threshold:
                continue
            p1, p2 = coords[left], coords[right]
            length = math.hypot(float(p2[0] - p1[0]), float(p2[1] - p1[1]))
            if length < min_length:
                continue
            confidence = edge_score * math.sqrt(float(scores[left] * scores[right]))
            entities.append(
                {
                    "type": "segment",
                    "line_class": "contour",
                    "width_class": "main",
                    "confidence": confidence,
                    "origin": "neural",
                    "assurance": "inferred",
                    "p1": {"x": float(p1[0]), "y": float(p1[1])},
                    "p2": {"x": float(p2[0]), "y": float(p2[1])},
                }
            )
    return entities
