"""DETR-style unordered primitive detector for technical drawings."""

from __future__ import annotations

import torch
import torch.nn as nn

from model import CnnEncoder
from primitive_dataset import LINE_CLASSES, N_PARAMS, TYPE_NAMES, WIDTH_CLASSES


class PrimitiveSetModel(nn.Module):
    def __init__(
        self,
        *,
        d_model: int = 128,
        n_queries: int = 96,
        n_layers: int = 3,
        n_heads: int = 8,
        dim_ff: int = 512,
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
        self.type_head = nn.Linear(d_model, len(TYPE_NAMES))
        self.param_head = nn.Sequential(nn.Linear(d_model, N_PARAMS), nn.Sigmoid())
        self.line_head = nn.Linear(d_model, len(LINE_CLASSES))
        self.width_head = nn.Linear(d_model, len(WIDTH_CLASSES))
        self.n_queries = n_queries

    def forward(self, images: torch.Tensor) -> dict[str, torch.Tensor]:
        memory = self.encoder(images)
        queries = self.query_embed.weight.unsqueeze(0).expand(images.size(0), -1, -1)
        hidden = self.decoder(queries, memory)
        return {
            "type_logits": self.type_head(hidden),
            "params": self.param_head(hidden),
            "line_logits": self.line_head(hidden),
            "width_logits": self.width_head(hidden),
        }
