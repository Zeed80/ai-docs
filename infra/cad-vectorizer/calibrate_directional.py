#!/usr/bin/env python3
"""Select directional proposal thresholds on source-grouped validation only."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import pathlib

import torch
from torch.utils.data import DataLoader

from directional_dataset import DirectionalFieldDataset
from directional_decode import decode_line_segments
from directional_model import DirectionalFieldModel


def _truth_segments(ir: dict) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    source = ir["source"]
    sx, sy = 256 / source["image_width"], 256 / source["image_height"]

    def point(raw):
        return raw["x"] * sx, raw["y"] * sy

    segments = []

    def append(p1, p2):
        segments.append((point(p1), point(p2)))

    for entity in ir.get("entities", []):
        kind = entity.get("type")
        if kind == "segment":
            append(entity["p1"], entity["p2"])
        elif kind == "polyline":
            points = entity.get("points", [])
            for p1, p2 in zip(points, points[1:], strict=False):
                append(p1, p2)
            if entity.get("closed") and len(points) > 2:
                append(points[-1], points[0])
        elif kind == "hatch":
            for points in [entity.get("boundary", []), *entity.get("holes", [])]:
                for p1, p2 in zip(points, [*points[1:], points[0]], strict=False):
                    append(p1, p2)
    return segments


def _distance(predicted: dict, truth) -> float:
    p1 = predicted["p1"]["x"], predicted["p1"]["y"]
    p2 = predicted["p2"]["x"], predicted["p2"]["y"]

    def d(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    return min(d(p1, truth[0]) + d(p2, truth[1]), d(p1, truth[1]) + d(p2, truth[0])) / 2


def _counts(predicted: list[dict], truth: list, tolerance: float = 2.5) -> tuple[int, int, int]:
    candidates = sorted(
        (
            (_distance(entity, segment), pred_index, truth_index)
            for pred_index, entity in enumerate(predicted)
            for truth_index, segment in enumerate(truth)
        ),
        key=lambda row: row[0],
    )
    used_pred, used_truth = set(), set()
    matched = 0
    for distance, pred_index, truth_index in candidates:
        if distance > tolerance:
            break
        if pred_index in used_pred or truth_index in used_truth:
            continue
        used_pred.add(pred_index)
        used_truth.add(truth_index)
        matched += 1
    return matched, len(predicted) - matched, len(truth) - matched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--checkpoint", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    dataset = DirectionalFieldDataset(args.data / "val.jsonl")
    loader = DataLoader(dataset, batch_size=12, shuffle=False)
    state = torch.load(args.checkpoint, map_location=device)
    model = DirectionalFieldModel().to(device)
    model.load_state_dict(state["model"])
    model.eval()
    outputs = []
    with torch.no_grad():
        for images, _targets in loader:
            outputs.extend(model(images.to(device)).cpu())
    truths = [
        _truth_segments(json.loads(pathlib.Path(row["ir"]).read_text()))
        for row in dataset.rows
    ]
    results = []
    for endpoint, line, support in itertools.product(
        (0.5, 0.6, 0.7),
        (0.4, 0.5, 0.6),
        (0.6, 0.7, 0.8),
    ):
        tp = fp = fn = 0
        for output, truth in zip(outputs, truths, strict=True):
            predicted = decode_line_segments(
                output,
                endpoint_threshold=endpoint,
                line_threshold=line,
                min_support=support,
            )
            matched, false_positive, false_negative = _counts(predicted, truth)
            tp += matched
            fp += false_positive
            fn += false_negative
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        results.append(
            {
                "endpoint_threshold": endpoint,
                "line_threshold": line,
                "min_support": support,
                "matched": tp,
                "false_positive": fp,
                "false_negative": fn,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
        )
    results.sort(key=lambda row: (row["f1"], row["precision"], row["recall"]), reverse=True)
    report = {
        "selection_split": "source_grouped_val",
        "entity_tolerance_px_at_256": 2.5,
        "best": results[0],
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["best"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
