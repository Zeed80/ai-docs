#!/usr/bin/env python3
"""Measure a frozen multi-type checkpoint on a named manifest split."""

from __future__ import annotations

import argparse
import json
import pathlib

import torch
from torch.utils.data import DataLoader

from multi_type_dataset import MultiTypeProposalDataset, collate_multi_type
from multi_type_model import MultiTypeProposalModel
from train_multi_type import evaluate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--split", default="holdout")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    state = torch.load(args.checkpoint, map_location=device)
    if state.get("architecture") != "multi-type-proposal-v2":
        raise SystemExit("checkpoint architecture mismatch")
    model = MultiTypeProposalModel(**state.get("model_config", {})).to(device)
    model.load_state_dict(state["model"])
    dataset = MultiTypeProposalDataset([args.manifest], split=args.split)
    if not dataset:
        raise SystemExit(f"empty split: {args.split}")
    loader = DataLoader(
        dataset, batch_size=args.batch_size, shuffle=False,
        num_workers=0, collate_fn=collate_multi_type,
    )
    metrics = evaluate(model, loader, device)
    report = {
        "architecture": state["architecture"],
        "checkpoint": str(args.checkpoint),
        "checkpoint_step": state.get("step"),
        "manifest": str(args.manifest),
        "split": args.split,
        "sheets": len(dataset),
        "metrics": metrics,
        "claim_scope": "proposal type and normalized geometry only; OCR payload and CadIR relations excluded",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
