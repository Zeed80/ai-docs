"""Exact node/edge graph targets for local CAD tiles."""

from __future__ import annotations

import json
import pathlib

import numpy as np
import torch
from PIL import Image
from torch.utils.data import Dataset

from model import IMG_SIZE

NODE_TYPES = ("none", "endpoint", "junction")
MAX_GRAPH_NODES = 96


def graph_target_from_ir(ir: dict) -> dict[str, torch.Tensor]:
    source = ir["source"]
    width = float(source["image_width"])
    height = float(source["image_height"])
    raw_segments: list[tuple[dict, dict]] = []

    def add(p1: dict, p2: dict) -> None:
        if p1["x"] == p2["x"] and p1["y"] == p2["y"]:
            return
        raw_segments.append((p1, p2))

    for entity in ir.get("entities", []):
        kind = entity.get("type")
        if kind == "segment":
            add(entity["p1"], entity["p2"])
        elif kind == "polyline":
            points = entity.get("points", [])
            for p1, p2 in zip(points, points[1:], strict=False):
                add(p1, p2)
            if entity.get("closed") and len(points) > 2:
                add(points[-1], points[0])
        elif kind == "hatch":
            for points in [entity.get("boundary", []), *entity.get("holes", [])]:
                for p1, p2 in zip(points, [*points[1:], points[0]], strict=False):
                    add(p1, p2)

    node_index: dict[tuple[float, float], int] = {}
    coordinates: list[tuple[float, float]] = []
    degrees: list[int] = []
    edges: set[tuple[int, int]] = set()

    def index(point: dict) -> int:
        normalized = point["x"] / width, point["y"] / height
        key = round(normalized[0], 5), round(normalized[1], 5)
        if key not in node_index:
            node_index[key] = len(coordinates)
            coordinates.append(normalized)
            degrees.append(0)
        return node_index[key]

    for p1, p2 in raw_segments:
        left, right = index(p1), index(p2)
        if left == right:
            continue
        edge = tuple(sorted((left, right)))
        if edge in edges:
            continue
        edges.add(edge)
        degrees[left] += 1
        degrees[right] += 1

    count = len(coordinates)
    adjacency = torch.zeros((count, count), dtype=torch.float32)
    for left, right in edges:
        adjacency[left, right] = adjacency[right, left] = 1
    return {
        "coords": torch.tensor(coordinates, dtype=torch.float32).reshape(-1, 2),
        "types": torch.tensor(
            [2 if degree > 1 else 1 for degree in degrees],
            dtype=torch.long,
        ),
        "adjacency": adjacency,
    }


class GraphDataset(Dataset):
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
        return torch.from_numpy(pixels).unsqueeze(0), graph_target_from_ir(ir)


def collate_graphs(batch):
    return torch.stack([item[0] for item in batch]), [item[1] for item in batch]
