"""Loads (image, sequence) pairs from build_dataset.py's jsonl manifests."""

from __future__ import annotations

import json
import pathlib

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset

from model import IMG_SIZE, N_PARAMS

_UNUSED = -1.0


class CadSequenceDataset(Dataset):
    def __init__(self, manifest_path: str | pathlib.Path):
        self.rows = []
        with open(manifest_path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self.rows.append(json.loads(line))

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, idx: int):
        row = self.rows[idx]
        img = Image.open(row["image"]).convert("L").resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS)
        arr = np.asarray(img, dtype=np.float32) / 255.0
        # Ink-as-signal: background (255) -> 0, dark ink -> ~1 (sparser, more
        # informative activation for a from-scratch CNN than raw grayscale).
        arr = 1.0 - arr
        image = torch.from_numpy(arr).unsqueeze(0)

        seq = np.load(row["sequence"]).astype(np.float32)  # (T, N_PARAMS+2+1) = (T, 8)
        cmd = torch.from_numpy(seq[:, 0]).long()
        params = torch.from_numpy(seq[:, 1 : 1 + N_PARAMS])
        params = torch.nan_to_num(params, nan=_UNUSED)
        lc = torch.from_numpy(seq[:, 1 + N_PARAMS]).long()
        wc = torch.from_numpy(seq[:, 2 + N_PARAMS]).long()
        return image, cmd, params, lc, wc


def collate(batch):
    """Pad variable-length sequences; prepend a BOS row (all-zero = EOS
    embedding, matches ``model.generate``'s BOS convention) to the DECODER
    INPUT side so teacher forcing predicts row t from rows[0..t-1]."""
    images = torch.stack([b[0] for b in batch])
    max_len = max(b[1].size(0) for b in batch) + 1  # +1 for BOS
    B = len(batch)
    cmd_in = torch.zeros(B, max_len, dtype=torch.long)
    params_in = torch.zeros(B, max_len, N_PARAMS)
    lc_in = torch.zeros(B, max_len, dtype=torch.long)
    wc_in = torch.zeros(B, max_len, dtype=torch.long)
    cmd_tgt = torch.zeros(B, max_len, dtype=torch.long)  # EOS-padded target
    params_tgt = torch.full((B, max_len, N_PARAMS), _UNUSED)
    lc_tgt = torch.zeros(B, max_len, dtype=torch.long)
    wc_tgt = torch.zeros(B, max_len, dtype=torch.long)
    pad_mask = torch.ones(B, max_len, dtype=torch.bool)  # True = valid target position

    for i, (_img, cmd, params, lc, wc) in enumerate(batch):
        t = cmd.size(0)
        cmd_in[i, 1 : 1 + t] = cmd
        params_in[i, 1 : 1 + t] = torch.nan_to_num(params, nan=0.0)
        lc_in[i, 1 : 1 + t] = lc
        wc_in[i, 1 : 1 + t] = wc
        cmd_tgt[i, :t] = cmd
        params_tgt[i, :t] = params
        lc_tgt[i, :t] = lc
        wc_tgt[i, :t] = wc
        pad_mask[i, t:] = False

    return images, (cmd_in, params_in, lc_in, wc_in), (cmd_tgt, params_tgt, lc_tgt, wc_tgt), pad_mask
