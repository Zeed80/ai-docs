"""DETR-like node set with a learned symmetric CAD adjacency matrix."""

from __future__ import annotations

import math

import torch
import torch.nn as nn

from graph_dataset import MAX_GRAPH_NODES, NODE_TYPES
from model import CnnEncoder


class CadGraphModel(nn.Module):
    def __init__(
        self,
        *,
        d_model: int = 128,
        n_queries: int = MAX_GRAPH_NODES,
        n_layers: int = 3,
        n_heads: int = 8,
        dim_ff: int = 512,
        edge_dim: int = 64,
    ):
        super().__init__()
        self.encoder = CnnEncoder(d_model)
        self.query_embed = nn.Embedding(n_queries, d_model)
        layer = nn.TransformerDecoderLayer(
            d_model=d_model,
            nhead=n_heads,
            dim_feedforward=dim_ff,
            dropout=0.1,
            batch_first=True,
        )
        self.decoder = nn.TransformerDecoder(layer, num_layers=n_layers)
        self.type_head = nn.Linear(d_model, len(NODE_TYPES))
        self.coord_head = nn.Sequential(nn.Linear(d_model, 2), nn.Sigmoid())
        self.edge_left = nn.Linear(d_model, edge_dim)
        self.edge_right = nn.Linear(d_model, edge_dim)
        self.edge_geometry = nn.Sequential(
            nn.Linear(3, 32),
            nn.ReLU(inplace=True),
            nn.Linear(32, 1),
        )
        self.n_queries = n_queries
        self.edge_dim = edge_dim

    def forward(self, images: torch.Tensor) -> dict[str, torch.Tensor]:
        memory = self.encoder(images)
        queries = self.query_embed.weight.unsqueeze(0).expand(images.size(0), -1, -1)
        hidden = self.decoder(queries, memory)
        coords = self.coord_head(hidden)
        left, right = self.edge_left(hidden), self.edge_right(hidden)
        directed = torch.matmul(left, right.transpose(1, 2)) / math.sqrt(self.edge_dim)
        pair_score = (directed + directed.transpose(1, 2)) / 2
        delta = (coords[:, :, None, :] - coords[:, None, :, :]).abs()
        distance = torch.linalg.vector_norm(delta, dim=-1, keepdim=True)
        geometry = torch.cat((delta, distance), dim=-1)
        edge_logits = pair_score + self.edge_geometry(geometry).squeeze(-1)
        diagonal = torch.eye(self.n_queries, device=images.device, dtype=torch.bool)
        edge_logits = edge_logits.masked_fill(diagonal[None], -20)
        return {
            "type_logits": self.type_head(hidden),
            "coords": coords,
            "edge_logits": edge_logits,
        }
