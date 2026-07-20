"""Fail-closed conversion of directional fields into line proposals."""

from __future__ import annotations

import math

import numpy as np
import torch
import torch.nn.functional as F


def _line_samples(
    array: np.ndarray,
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> np.ndarray:
    length = max(int(math.hypot(p2[0] - p1[0], p2[1] - p1[1])) + 1, 2)
    x = np.linspace(p1[0], p2[0], length).round().astype(int)
    y = np.linspace(p1[1], p2[1], length).round().astype(int)
    x = np.clip(x, 0, array.shape[1] - 1)
    y = np.clip(y, 0, array.shape[0] - 1)
    return array[y, x]


def decode_line_segments(
    output: torch.Tensor,
    *,
    endpoint_threshold: float = 0.7,
    line_threshold: float = 0.4,
    min_length: float = 4.0,
    min_support: float = 0.6,
    max_peaks: int = 128,
    max_segments: int = 256,
) -> list[dict]:
    """Decode one CHW model output; no source raster or CV fallback is used."""

    if output.ndim != 3:
        raise ValueError("expected CHW directional output")
    heatmaps = output[:6].sigmoid()
    point_map = torch.maximum(heatmaps[1], heatmaps[2])
    maxima = F.max_pool2d(point_map[None, None], 7, stride=1, padding=3)[0, 0]
    peak_mask = (point_map >= endpoint_threshold) & (point_map >= maxima - 1e-7)
    ys, xs = torch.where(peak_mask)
    if not len(xs):
        return []
    scores = point_map[ys, xs]
    order = scores.argsort(descending=True)[:max_peaks]
    peaks: list[tuple[float, float, float]] = []
    for selected in order.tolist():
        candidate = (float(xs[selected]), float(ys[selected]), float(scores[selected]))
        if any(
            math.hypot(candidate[0] - prior[0], candidate[1] - prior[1]) < 5
            for prior in peaks
        ):
            continue
        peaks.append(candidate)
    line = heatmaps[0].cpu().numpy()
    direction = F.normalize(output[6:8], dim=0, eps=1e-6).cpu().numpy()
    candidates: list[tuple[float, dict]] = []
    for index, (x1, y1, score1) in enumerate(peaks):
        for x2, y2, score2 in peaks[index + 1 :]:
            distance = math.hypot(x2 - x1, y2 - y1)
            if distance < min_length:
                continue
            # A nearer endpoint/junction lying on the same stroke terminates
            # the primitive.  Without this graph rule every collinear pair
            # becomes a segment and proposal count grows quadratically.
            dx, dy = x2 - x1, y2 - y1
            has_intermediate_vertex = False
            for x3, y3, _score3 in peaks:
                projection = ((x3 - x1) * dx + (y3 - y1) * dy) / (distance * distance)
                if not 0.05 < projection < 0.95:
                    continue
                projected_x = x1 + projection * dx
                projected_y = y1 + projection * dy
                if math.hypot(x3 - projected_x, y3 - projected_y) <= 4:
                    has_intermediate_vertex = True
                    break
            if has_intermediate_vertex:
                continue
            theta = math.atan2(y2 - y1, x2 - x1)
            expected = np.array((math.cos(2 * theta), math.sin(2 * theta)))
            samples = _line_samples(line, (x1, y1), (x2, y2))
            support = float((samples >= line_threshold).mean())
            if support < min_support:
                continue
            direction_x = _line_samples(direction[0], (x1, y1), (x2, y2))
            direction_y = _line_samples(direction[1], (x1, y1), (x2, y2))
            agreement = float(
                np.mean(direction_x * expected[0] + direction_y * expected[1])
            )
            if agreement < 0.7:
                continue
            score = math.sqrt(score1 * score2) * support * agreement
            candidates.append(
                (
                    score,
                    {
                        "type": "segment",
                        "line_class": "contour",
                        "width_class": "main",
                        "confidence": score,
                        "origin": "neural",
                        "assurance": "inferred",
                        "p1": {"x": x1, "y": y1},
                        "p2": {"x": x2, "y": y2},
                    },
                )
            )
    candidates.sort(key=lambda item: item[0], reverse=True)
    kept: list[dict] = []
    used_pairs: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for _score, entity in candidates:
        p1, p2 = entity["p1"], entity["p2"]
        pair = tuple(
            sorted(
                (
                    (round(p1["x"]), round(p1["y"])),
                    (round(p2["x"]), round(p2["y"])),
                )
            )
        )
        if pair in used_pairs:
            continue
        used_pairs.add(pair)
        kept.append(entity)
        if len(kept) >= max_segments:
            break
    return kept
