"""Set-based primitive targets for exact local CAD tiles.

Unlike the legacy command sequence, a target is an unordered set.  The model
therefore has no EOS token, no ordering dependency and no 200-command sheet
limit.  Polylines and hatch loops are expanded to segments so the first
detector has one unambiguous geometric contract: SEGMENT, CIRCLE and ARC.
Text and dimensions remain in their dedicated OCR/relation path.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset

from model import IMG_SIZE

TYPE_NAMES = ("none", "segment", "circle", "arc")
TYPE_INDEX = {name: index for index, name in enumerate(TYPE_NAMES)}
LINE_CLASSES = ("contour", "axis", "dim", "hatch", "hidden", "thin")
WIDTH_CLASSES = ("main", "thin")
N_PARAMS = 5


def _style(entity: dict) -> tuple[int, int]:
    line_class = entity.get("line_class", "contour")
    width_class = entity.get("width_class", "main")
    return (
        LINE_CLASSES.index(line_class) if line_class in LINE_CLASSES else 0,
        WIDTH_CLASSES.index(width_class) if width_class in WIDTH_CLASSES else 0,
    )


def targets_from_ir_dict(ir: dict) -> dict[str, torch.Tensor]:
    """Convert CadIR JSON into normalized unordered primitive targets."""
    source = ir["source"]
    width = float(source["image_width"])
    height = float(source["image_height"])
    radius_scale = max(width, height)
    types: list[int] = []
    params: list[list[float]] = []
    line_classes: list[int] = []
    width_classes: list[int] = []

    def add(kind: str, values: list[float], entity: dict) -> None:
        line_class, width_class = _style(entity)
        types.append(TYPE_INDEX[kind])
        params.append(values + [0.0] * (N_PARAMS - len(values)))
        line_classes.append(line_class)
        width_classes.append(width_class)

    def add_segment(p1: dict, p2: dict, entity: dict) -> None:
        add(
            "segment",
            [p1["x"] / width, p1["y"] / height, p2["x"] / width, p2["y"] / height],
            entity,
        )

    for entity in ir.get("entities", []):
        kind = entity.get("type")
        if kind == "segment":
            add_segment(entity["p1"], entity["p2"], entity)
        elif kind == "circle":
            center = entity["center"]
            add(
                "circle",
                [
                    center["x"] / width,
                    center["y"] / height,
                    entity["radius"] / radius_scale,
                ],
                entity,
            )
        elif kind == "arc":
            center = entity["center"]
            add(
                "arc",
                [
                    center["x"] / width,
                    center["y"] / height,
                    entity["radius"] / radius_scale,
                    entity["start_angle"] / 360.0,
                    entity["end_angle"] / 360.0,
                ],
                entity,
            )
        elif kind == "polyline":
            points = entity.get("points", [])
            pairs = list(zip(points, points[1:], strict=False))
            if entity.get("closed") and len(points) > 2:
                pairs.append((points[-1], points[0]))
            for p1, p2 in pairs:
                add_segment(p1, p2, entity)
        elif kind == "hatch":
            loops = [entity.get("boundary", []), *entity.get("holes", [])]
            for points in loops:
                if len(points) < 2:
                    continue
                for p1, p2 in zip(points, [*points[1:], points[0]], strict=False):
                    add_segment(p1, p2, entity)

    return {
        "types": torch.tensor(types, dtype=torch.long),
        "params": torch.tensor(params, dtype=torch.float32).reshape(-1, N_PARAMS),
        "line_classes": torch.tensor(line_classes, dtype=torch.long),
        "width_classes": torch.tensor(width_classes, dtype=torch.long),
    }


class PrimitiveSetDataset(Dataset):
    def __init__(self, manifest_path: str | pathlib.Path):
        self.rows = [
            json.loads(line)
            for line in pathlib.Path(manifest_path).read_text().splitlines()
            if line.strip()
        ]

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int):
        row = self.rows[index]
        image = Image.open(row["image"]).convert("L").resize(
            (IMG_SIZE, IMG_SIZE), Image.Resampling.LANCZOS
        )
        pixels = 1.0 - np.asarray(image, dtype=np.float32) / 255.0
        tensor = torch.from_numpy(pixels).unsqueeze(0)
        ir = json.loads(pathlib.Path(row["ir"]).read_text())
        return tensor, targets_from_ir_dict(ir)


def collate_primitive_sets(batch):
    return torch.stack([item[0] for item in batch]), [item[1] for item in batch]
