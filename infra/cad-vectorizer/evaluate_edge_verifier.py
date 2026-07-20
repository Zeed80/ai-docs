#!/usr/bin/env python3
"""Open the independent holdout for an already selected edge verifier."""

from __future__ import annotations

import argparse
import json
import pathlib

import torch
from torch.utils.data import DataLoader

from directional_model import DirectionalFieldModel
from edge_verifier import EdgeVerifier
from graph_dataset import GraphDataset, collate_graphs
from train_edge_verifier import evaluate_entities


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--directional-checkpoint", required=True, type=pathlib.Path)
    parser.add_argument("--checkpoint", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    directional_state = torch.load(args.directional_checkpoint, map_location=device)
    field_model = DirectionalFieldModel().to(device)
    field_model.load_state_dict(directional_state["model"])
    field_model.eval()
    state = torch.load(args.checkpoint, map_location=device)
    verifier = EdgeVerifier().to(device)
    verifier.load_state_dict(state["model"])
    verifier.eval()
    selected = state["validation_entity"]
    holdout = evaluate_entities(
        field_model,
        verifier,
        DataLoader(
            GraphDataset(args.data / "holdout.jsonl"),
            batch_size=1,
            shuffle=False,
            collate_fn=collate_graphs,
        ),
        device,
        thresholds=(selected["node_threshold"], selected["edge_threshold"]),
    )
    report = {
        "architecture": "dense-edge-verifier-v1",
        "selection_split": "source_grouped_val",
        "best_epoch": state["epoch"],
        "best_validation": selected,
        "independent_real_holdout": holdout,
    }
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(holdout, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
