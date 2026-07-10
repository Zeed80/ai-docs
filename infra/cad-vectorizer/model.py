"""Drawing2CAD-class neural vectorizer: CNN encoder + autoregressive
Transformer decoder over CAD IR command ROWS (not raw pixels/points).

Each decoding step predicts one full row of ``app.ai.cad_ir.sequence``:
(command, 5 continuous params, line_class, width_class) — matching PHT-CAD's
"regression heads per primitive" style rather than DeepCAD's per-scalar
token quantization, because it keeps sequences short (tens of rows per
sheet, not hundreds of point-tokens) and lets us reuse ``sequence.decode()``
unmodified to turn model output straight back into IR entities.

Trained from scratch (no pretrained weights exist for this custom vocabulary
— Drawing2CAD/PHT-CAD's own checkpoints are not published, see project
research notes) on our own synthetic + real-DWG-holdout dataset.
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn

N_COMMANDS = 7  # EOS SEG ARC CIR PLN PT HAT (cad_ir.sequence.COMMANDS)
N_LINE_CLASSES = 6  # contour axis dim hatch hidden thin
N_WIDTH_CLASSES = 2  # main thin
N_PARAMS = 5  # continuous slots per row (cad_ir.sequence.N_PARAMS - 2)

IMG_SIZE = 256
MAX_SEQ_LEN = 200  # sheets rarely exceed this many primitives+points


class CnnEncoder(nn.Module):
    """Grayscale line-drawing -> grid of visual tokens. Trained from scratch:
    line drawings are a different visual domain than natural images, so an
    ImageNet backbone buys little and costs a lot of unnecessary parameters
    for a single-GPU from-scratch training budget."""

    def __init__(self, d_model: int = 256):
        super().__init__()
        ch = [1, 32, 64, 128, d_model]
        blocks = []
        for i in range(4):
            blocks.append(nn.Sequential(
                nn.Conv2d(ch[i], ch[i + 1], 3, stride=2, padding=1),
                nn.BatchNorm2d(ch[i + 1]),
                nn.ReLU(inplace=True),
                nn.Conv2d(ch[i + 1], ch[i + 1], 3, stride=1, padding=1),
                nn.BatchNorm2d(ch[i + 1]),
                nn.ReLU(inplace=True),
            ))
        self.blocks = nn.ModuleList(blocks)
        # IMG_SIZE / 2**4 grid side (256 -> 16)
        grid = IMG_SIZE // 16
        self.pos_embed = nn.Parameter(torch.zeros(1, grid * grid, d_model))
        nn.init.trunc_normal_(self.pos_embed, std=0.02)
        self.d_model = d_model

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        for block in self.blocks:
            x = block(x)
        b, c, h, w = x.shape
        tokens = x.flatten(2).transpose(1, 2)  # (B, H*W, C)
        return tokens + self.pos_embed[:, : h * w, :]


class RowEmbedding(nn.Module):
    """Embeds one target/predicted row (cmd, 5 params, line_class,
    width_class) into a single decoder input vector."""

    def __init__(self, d_model: int = 256):
        super().__init__()
        self.cmd_embed = nn.Embedding(N_COMMANDS, d_model)
        self.lc_embed = nn.Embedding(N_LINE_CLASSES, d_model)
        self.wc_embed = nn.Embedding(N_WIDTH_CLASSES, d_model)
        self.param_proj = nn.Linear(N_PARAMS, d_model)
        self.merge = nn.Linear(d_model * 4, d_model)

    def forward(self, cmd, params, lc, wc):
        e = torch.cat(
            [self.cmd_embed(cmd), self.param_proj(params), self.lc_embed(lc), self.wc_embed(wc)],
            dim=-1,
        )
        return self.merge(e)


class SinusoidalPositionalEncoding(nn.Module):
    def __init__(self, d_model: int, max_len: int = MAX_SEQ_LEN + 1):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        pos = torch.arange(0, max_len, dtype=torch.float32).unsqueeze(1)
        div = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(0))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.pe[:, : x.size(1), :]


class CadVectorizerModel(nn.Module):
    def __init__(self, d_model: int = 256, n_layers: int = 4, n_heads: int = 8, dim_ff: int = 1024):
        super().__init__()
        self.encoder = CnnEncoder(d_model)
        self.row_embed = RowEmbedding(d_model)
        self.pos_enc = SinusoidalPositionalEncoding(d_model)
        decoder_layer = nn.TransformerDecoderLayer(
            d_model=d_model, nhead=n_heads, dim_feedforward=dim_ff,
            dropout=0.1, batch_first=True,
        )
        self.decoder = nn.TransformerDecoder(decoder_layer, num_layers=n_layers)
        self.cmd_head = nn.Linear(d_model, N_COMMANDS)
        self.param_head = nn.Linear(d_model, N_PARAMS)
        self.lc_head = nn.Linear(d_model, N_LINE_CLASSES)
        self.wc_head = nn.Linear(d_model, N_WIDTH_CLASSES)
        self.d_model = d_model

    @staticmethod
    def causal_mask(n: int, device) -> torch.Tensor:
        return torch.triu(torch.full((n, n), float("-inf"), device=device), diagonal=1)

    def forward(self, image: torch.Tensor, cmd, params, lc, wc):
        """Teacher-forced training pass. Inputs are the SHIFTED-RIGHT target
        rows (a learned <BOS> row is row 0); returns logits/params aligned to
        predict the ORIGINAL (unshifted) target at each position."""
        memory = self.encoder(image)
        tgt = self.pos_enc(self.row_embed(cmd, params, lc, wc))
        mask = self.causal_mask(tgt.size(1), tgt.device)
        hidden = self.decoder(tgt, memory, tgt_mask=mask)
        return (
            self.cmd_head(hidden), self.param_head(hidden),
            self.lc_head(hidden), self.wc_head(hidden),
        )

    @torch.no_grad()
    def generate(self, image: torch.Tensor, max_len: int = MAX_SEQ_LEN, device=None):
        """Greedy autoregressive decoding for a SINGLE image (B=1). Returns
        rows as a list of (cmd_idx, params[5], lc_idx, wc_idx) tuples, EOS-
        terminated or truncated at ``max_len``."""
        device = device or image.device
        memory = self.encoder(image)
        cmd = torch.zeros(1, 1, dtype=torch.long, device=device)  # BOS reuses EOS(=0) embedding slot
        params = torch.zeros(1, 1, N_PARAMS, device=device)
        lc = torch.zeros(1, 1, dtype=torch.long, device=device)
        wc = torch.zeros(1, 1, dtype=torch.long, device=device)
        rows: list[tuple[int, list[float], int, int]] = []
        for _step in range(max_len):
            tgt = self.pos_enc(self.row_embed(cmd, params, lc, wc))
            mask = self.causal_mask(tgt.size(1), device)
            hidden = self.decoder(tgt, memory, tgt_mask=mask)
            last = hidden[:, -1, :]
            cmd_logits = self.cmd_head(last)
            next_cmd = cmd_logits.argmax(-1)
            next_params = self.param_head(last)
            next_lc = self.lc_head(last).argmax(-1)
            next_wc = self.wc_head(last).argmax(-1)
            cmd_i = int(next_cmd.item())
            if cmd_i == 0:  # EOS predicted (incl. at step 0 = "no entities") -> stop, don't emit it as a row
                break
            rows.append((cmd_i, next_params[0].tolist(), int(next_lc.item()), int(next_wc.item())))
            cmd = torch.cat([cmd, next_cmd.unsqueeze(1)], dim=1)
            params = torch.cat([params, next_params.unsqueeze(1)], dim=1)
            lc = torch.cat([lc, next_lc.unsqueeze(1)], dim=1)
            wc = torch.cat([wc, next_wc.unsqueeze(1)], dim=1)
        return rows
