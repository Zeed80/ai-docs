from __future__ import annotations

import pathlib
import sys

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from edge_verifier import EdgeVerifier, detect_nodes, pair_features  # noqa: E402


def test_pair_features_measure_line_and_direction_support() -> None:
    output = torch.full((9, 64, 64), -10.0)
    output[6:] = 0
    output[0, 30, 8:57] = 10
    output[1, 30, 8] = output[1, 30, 56] = 10
    output[6, 30, 8:57] = 1
    pair = torch.tensor([[8 / 63, 30 / 63, 56 / 63, 30 / 63]])
    features = pair_features(output, pair)
    assert features.shape == (1, 14)
    assert float(features[0, 4]) > 0.9
    assert float(features[0, 9]) > 0.9


def test_detect_nodes_returns_subpixel_normalized_peaks() -> None:
    output = torch.full((9, 32, 32), -10.0)
    output[1, 10:13, 15:18] = 8
    nodes = detect_nodes(output, threshold=0.6)
    assert nodes.shape == (1, 2)
    assert 0.49 < float(nodes[0, 0]) < 0.53
    assert 0.33 < float(nodes[0, 1]) < 0.38
    assert EdgeVerifier()(torch.rand(2, 14)).shape == (2,)
