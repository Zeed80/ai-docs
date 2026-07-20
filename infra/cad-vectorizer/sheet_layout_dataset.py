"""Dataset contract for global orthographic-view layout recognition."""

from __future__ import annotations

import json
import pathlib

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset

from model import IMG_SIZE

VIEW_NAMES = ("none", "view")
VIEW_INDEX = {name: index for index, name in enumerate(VIEW_NAMES)}


def target_from_row(row: dict) -> dict[str, torch.Tensor]:
    width, height = float(row["width"]), float(row["height"])
    types = []
    boxes = []
    for target in row["targets"]:
        x0, y0, x1, y1 = target["box"]
        types.append(VIEW_INDEX[target["kind"]])
        boxes.append(
            [
                ((x0 + x1) / 2) / width,
                ((y0 + y1) / 2) / height,
                (x1 - x0) / width,
                (y1 - y0) / height,
            ]
        )
    return {
        "types": torch.tensor(types, dtype=torch.long),
        "boxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
    }


class SheetLayoutDataset(Dataset):
    def __init__(self, manifest_path: str | pathlib.Path, split: str):
        self.rows = [
            row
            for row in (
                json.loads(line)
                for line in pathlib.Path(manifest_path).read_text().splitlines()
                if line.strip()
            )
            if row["split"] == split
        ]

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int):
        row = self.rows[index]
        image = Image.open(row["image"]).convert("L").resize(
            (IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS
        )
        pixels = 1.0 - np.asarray(image, dtype=np.float32) / 255.0
        return torch.from_numpy(pixels).unsqueeze(0), target_from_row(row)


def collate_sheet_layout(batch):
    return torch.stack([item[0] for item in batch]), [item[1] for item in batch]
