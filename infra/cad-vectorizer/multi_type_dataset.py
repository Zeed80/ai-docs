"""Full-CadIR unordered targets for the multi-type proposal candidate.

Geometry types carry complete parameters. Semantic types deliberately carry
only anchors/subtypes: OCR and relation assembly remain separate stages and
must fill text/value payloads before a proposal can become exact CadIR.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset

from model import IMG_SIZE
from primitive_dataset import LINE_CLASSES, WIDTH_CLASSES

TYPE_NAMES = (
    "none",
    "segment",
    "circle",
    "arc",
    "text",
    "dimension",
    "annotation",
    "hatch",
)
TYPE_INDEX = {name: index for index, name in enumerate(TYPE_NAMES)}
SUBTYPE_NAMES = (
    "none",
    "linear",
    "diameter",
    "radial",
    "angular",
    "roughness",
    "thread",
    "tolerance",
    "datum",
    "weld",
    "ansi31",
    "solid",
)
SUBTYPE_INDEX = {name: index for index, name in enumerate(SUBTYPE_NAMES)}
N_PARAMS = 8


def _style(entity: dict) -> tuple[int, int]:
    line_class = entity.get("line_class", "contour")
    width_class = entity.get("width_class", "main")
    return (
        LINE_CLASSES.index(line_class) if line_class in LINE_CLASSES else 0,
        WIDTH_CLASSES.index(width_class) if width_class in WIDTH_CLASSES else 0,
    )


def targets_from_ir_dict(ir: dict) -> dict[str, torch.Tensor]:
    source = ir["source"]
    width = float(source["image_width"])
    height = float(source["image_height"])
    radius_scale = max(width, height)
    types: list[int] = []
    params: list[list[float]] = []
    line_classes: list[int] = []
    width_classes: list[int] = []
    subtypes: list[int] = []

    def add(
        kind: str,
        values: list[float],
        entity: dict,
        subtype: str = "none",
    ) -> None:
        line_class, width_class = _style(entity)
        types.append(TYPE_INDEX[kind])
        params.append(values + [0.0] * (N_PARAMS - len(values)))
        line_classes.append(line_class)
        width_classes.append(width_class)
        subtypes.append(SUBTYPE_INDEX.get(subtype, 0))

    def point(value: dict) -> tuple[float, float]:
        return value["x"] / width, value["y"] / height

    def add_segment(p1: dict, p2: dict, entity: dict) -> None:
        x1, y1 = point(p1)
        x2, y2 = point(p2)
        add("segment", [x1, y1, x2, y2], entity)

    for entity in ir.get("entities", []):
        kind = entity.get("type")
        if kind == "segment":
            add_segment(entity["p1"], entity["p2"], entity)
        elif kind == "circle":
            cx, cy = point(entity["center"])
            add("circle", [cx, cy, entity["radius"] / radius_scale], entity)
        elif kind == "arc":
            cx, cy = point(entity["center"])
            add(
                "arc",
                [
                    cx,
                    cy,
                    entity["radius"] / radius_scale,
                    entity["start_angle"] % 360.0 / 360.0,
                    entity["end_angle"] % 360.0 / 360.0,
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
        elif kind == "text":
            x, y = point(entity["position"])
            add(
                "text",
                [
                    x,
                    y,
                    float(entity.get("height", 3.5)) / radius_scale,
                    float(entity.get("rotation", 0.0)) % 360.0 / 360.0,
                ],
                entity,
            )
        elif kind == "dimension":
            x1, y1 = point(entity["p1"])
            x2, y2 = point(entity["p2"])
            add(
                "dimension",
                [x1, y1, x2, y2],
                entity,
                str(entity.get("kind", "linear")),
            )
        elif kind == "annotation":
            x, y = point(entity["position"])
            leader = entity.get("leader") or entity["position"]
            lx, ly = point(leader)
            add(
                "annotation",
                [x, y, lx, ly],
                entity,
                str(entity.get("kind", "roughness")),
            )
        elif kind == "hatch":
            boundary = entity.get("boundary", [])
            if len(boundary) < 3:
                continue
            xs = [float(item["x"]) for item in boundary]
            ys = [float(item["y"]) for item in boundary]
            add(
                "hatch",
                [min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height],
                entity,
                str(entity.get("pattern", "ansi31")),
            )

    return {
        "types": torch.tensor(types, dtype=torch.long),
        "params": torch.tensor(params, dtype=torch.float32).reshape(-1, N_PARAMS),
        "line_classes": torch.tensor(line_classes, dtype=torch.long),
        "width_classes": torch.tensor(width_classes, dtype=torch.long),
        "subtypes": torch.tensor(subtypes, dtype=torch.long),
    }


class MultiTypeProposalDataset(Dataset):
    def __init__(self, manifest_paths: list[str | pathlib.Path], *, split: str):
        rows = []
        for raw_path in manifest_paths:
            path = pathlib.Path(raw_path)
            rows.extend(
                json.loads(line)
                for line in path.read_text().splitlines()
                if line.strip()
            )
        self.rows = [row for row in rows if row.get("split") == split]

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


def collate_multi_type(batch):
    return torch.stack([item[0] for item in batch]), [item[1] for item in batch]
