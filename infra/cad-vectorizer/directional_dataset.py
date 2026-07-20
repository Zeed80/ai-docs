"""Directional geometry fields derived from exact CAD IR.

Heatmaps locate geometric evidence.  Continuous fields retain orientation
and radius without regressing an arbitrary fixed-size primitive list.
"""

from __future__ import annotations

import json
import math
import pathlib
from collections import Counter

import numpy as np
import torch
from PIL import Image, ImageDraw
from torch.utils.data import Dataset

from model import IMG_SIZE

HEATMAP_NAMES = ("line", "endpoint", "junction", "circle", "arc", "center")
FIELD_NAMES = ("direction_cos2", "direction_sin2", "radius")
N_OUTPUT_CHANNELS = len(HEATMAP_NAMES) + len(FIELD_NAMES)


def _disk(draw: ImageDraw.ImageDraw, point: tuple[float, float], radius: int = 2) -> None:
    x, y = point
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=255)


def directional_target_from_ir(ir: dict) -> torch.Tensor:
    source = ir["source"]
    sx = IMG_SIZE / float(source["image_width"])
    sy = IMG_SIZE / float(source["image_height"])
    radius_scale = (sx + sy) / 2
    heatmaps = [Image.new("L", (IMG_SIZE, IMG_SIZE), 0) for _ in HEATMAP_NAMES]
    draws = [ImageDraw.Draw(mask) for mask in heatmaps]
    direction_x = np.zeros((IMG_SIZE, IMG_SIZE), dtype=np.float32)
    direction_y = np.zeros_like(direction_x)
    direction_count = np.zeros_like(direction_x)
    radius_field = np.zeros_like(direction_x)
    endpoints: list[tuple[float, float]] = []

    def point(raw: dict) -> tuple[float, float]:
        return raw["x"] * sx, raw["y"] * sy

    def segment(p1_raw: dict, p2_raw: dict) -> None:
        p1, p2 = point(p1_raw), point(p2_raw)
        draws[0].line((*p1, *p2), fill=255, width=2)
        _disk(draws[1], p1)
        _disk(draws[1], p2)
        endpoints.extend((p1, p2))
        theta = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
        local = Image.new("L", (IMG_SIZE, IMG_SIZE), 0)
        ImageDraw.Draw(local).line((*p1, *p2), fill=255, width=2)
        selected = np.asarray(local) > 0
        direction_x[selected] += math.cos(2 * theta)
        direction_y[selected] += math.sin(2 * theta)
        direction_count[selected] += 1

    for entity in ir.get("entities", []):
        kind = entity.get("type")
        if kind == "segment":
            segment(entity["p1"], entity["p2"])
        elif kind == "polyline":
            points = entity.get("points", [])
            for p1, p2 in zip(points, points[1:], strict=False):
                segment(p1, p2)
            if entity.get("closed") and len(points) > 2:
                segment(points[-1], points[0])
        elif kind == "hatch":
            loops = [entity.get("boundary", []), *entity.get("holes", [])]
            for points in loops:
                for p1, p2 in zip(points, [*points[1:], points[0]], strict=False):
                    segment(p1, p2)
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
                draws[3].ellipse(box, outline=255, width=2)
            else:
                draws[4].arc(
                    box,
                    start=entity["start_angle"],
                    end=entity["end_angle"],
                    fill=255,
                    width=2,
                )
                for angle in (entity["start_angle"], entity["end_angle"]):
                    radians = math.radians(angle)
                    endpoint = (
                        center[0] + radius * math.cos(radians),
                        center[1] + radius * math.sin(radians),
                    )
                    _disk(draws[1], endpoint)
                    endpoints.append(endpoint)
            _disk(draws[5], center)
            center_mask = Image.new("L", (IMG_SIZE, IMG_SIZE), 0)
            _disk(ImageDraw.Draw(center_mask), center)
            radius_field[np.asarray(center_mask) > 0] = min(radius / IMG_SIZE, 1.0)

    endpoint_counts = Counter((round(x), round(y)) for x, y in endpoints)
    for (x, y), count in endpoint_counts.items():
        if count >= 2:
            _disk(draws[2], (x, y), radius=3)

    valid = direction_count > 0
    magnitude = np.hypot(direction_x, direction_y)
    stable = valid & (magnitude > 1e-6)
    direction_x[stable] /= magnitude[stable]
    direction_y[stable] /= magnitude[stable]
    masks = np.stack(
        [np.asarray(mask, dtype=np.float32) / 255.0 for mask in heatmaps],
        axis=0,
    )
    fields = np.stack((direction_x, direction_y, radius_field), axis=0)
    return torch.from_numpy(np.concatenate((masks, fields), axis=0))


class DirectionalFieldDataset(Dataset):
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
        return torch.from_numpy(pixels).unsqueeze(0), directional_target_from_ir(ir)
