"""Raster-evidence targets derived from exact CAD IR geometry."""

from __future__ import annotations

import json
import pathlib

import numpy as np
import torch
from PIL import Image, ImageDraw
from torch.utils.data import Dataset

from model import IMG_SIZE

EVIDENCE_NAMES = ("line", "circle", "arc")


def evidence_target_from_ir(ir: dict) -> torch.Tensor:
    source = ir["source"]
    sx = IMG_SIZE / float(source["image_width"])
    sy = IMG_SIZE / float(source["image_height"])
    radius_scale = (sx + sy) / 2
    masks = [Image.new("L", (IMG_SIZE, IMG_SIZE), 0) for _ in EVIDENCE_NAMES]
    draws = [ImageDraw.Draw(mask) for mask in masks]

    def point(raw: dict) -> tuple[float, float]:
        return raw["x"] * sx, raw["y"] * sy

    def line(p1: dict, p2: dict) -> None:
        draws[0].line((*point(p1), *point(p2)), fill=255, width=2)

    for entity in ir.get("entities", []):
        kind = entity.get("type")
        if kind == "segment":
            line(entity["p1"], entity["p2"])
        elif kind == "polyline":
            points = entity.get("points", [])
            for p1, p2 in zip(points, points[1:], strict=False):
                line(p1, p2)
            if entity.get("closed") and len(points) > 2:
                line(points[-1], points[0])
        elif kind == "hatch":
            loops = [entity.get("boundary", []), *entity.get("holes", [])]
            for points in loops:
                for p1, p2 in zip(points, [*points[1:], points[0]], strict=False):
                    line(p1, p2)
        elif kind in ("circle", "arc"):
            center = point(entity["center"])
            radius = entity["radius"] * radius_scale
            box = (
                center[0] - radius,
                center[1] - radius,
                center[0] + radius,
                center[1] + radius,
            )
            if kind == "circle":
                draws[1].ellipse(box, outline=255, width=2)
            else:
                draws[2].arc(
                    box,
                    start=entity["start_angle"],
                    end=entity["end_angle"],
                    fill=255,
                    width=2,
                )
    array = np.stack(
        [np.asarray(mask, dtype=np.float32) / 255.0 for mask in masks],
        axis=0,
    )
    return torch.from_numpy(array)


class EvidenceDataset(Dataset):
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
        ir = json.loads(pathlib.Path(row["ir"]).read_text())
        return torch.from_numpy(pixels).unsqueeze(0), evidence_target_from_ir(ir)
