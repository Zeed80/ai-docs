"""Small DETR-style global detector for orthographic view regions."""

from __future__ import annotations

import torch
import torch.nn as nn

from model import CnnEncoder
from sheet_layout_dataset import VIEW_NAMES


class SheetLayoutModel(nn.Module):
    def __init__(
        self,
        *,
        d_model: int = 128,
        n_queries: int = 6,
        n_layers: int = 2,
        n_heads: int = 8,
        dim_ff: int = 384,
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
        self.type_head = nn.Linear(d_model, len(VIEW_NAMES))
        self.box_head = nn.Sequential(nn.Linear(d_model, 4), nn.Sigmoid())

    def forward(self, images: torch.Tensor) -> dict[str, torch.Tensor]:
        memory = self.encoder(images)
        queries = self.query_embed.weight.unsqueeze(0).expand(images.size(0), -1, -1)
        hidden = self.decoder(queries, memory)
        return {
            "type_logits": self.type_head(hidden),
            "boxes": self.box_head(hidden),
        }
