#!/usr/bin/env python3
"""Train an unordered CAD node/edge graph and select by entity F1."""

from __future__ import annotations

import argparse
import itertools
import json
import math
import pathlib
import time

import torch
from scipy.optimize import linear_sum_assignment
from torch.utils.data import DataLoader

from graph_dataset import GraphDataset, collate_graphs
from graph_decode import decode_graph_segments
from graph_loss import cad_graph_loss
from graph_model import CadGraphModel


def _truth_edges(target: dict[str, torch.Tensor]) -> list[dict]:
    coords = target["coords"]
    adjacency = target["adjacency"]
    entities = []
    for left in range(len(coords)):
        for right in range(left + 1, len(coords)):
            if not adjacency[left, right]:
                continue
            entities.append(
                {
                    "p1": {"x": float(coords[left, 0]), "y": float(coords[left, 1])},
                    "p2": {"x": float(coords[right, 0]), "y": float(coords[right, 1])},
                }
            )
    return entities


def _entity_distance(left: dict, right: dict) -> float:
    lp1 = left["p1"]["x"], left["p1"]["y"]
    lp2 = left["p2"]["x"], left["p2"]["y"]
    rp1 = right["p1"]["x"], right["p1"]["y"]
    rp2 = right["p2"]["x"], right["p2"]["y"]

    def distance(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    direct = max(distance(lp1, rp1), distance(lp2, rp2))
    reverse = max(distance(lp1, rp2), distance(lp2, rp1))
    return min(direct, reverse)


def _counts(predicted: list[dict], truth: list[dict], tolerance: float = 0.0025):
    if not predicted or not truth:
        return 0, len(predicted), len(truth)
    costs = torch.tensor(
        [[_entity_distance(left, right) for right in truth] for left in predicted]
    ).numpy()
    rows, columns = linear_sum_assignment(costs)
    matched = sum(float(costs[row, column]) <= tolerance for row, column in zip(rows, columns))
    return matched, len(predicted) - matched, len(truth) - matched


@torch.no_grad()
def evaluate(
    model,
    loader,
    device,
    *,
    thresholds: tuple[float, float] | None = None,
) -> dict:
    model.eval()
    threshold_grid = (
        [thresholds]
        if thresholds
        else list(itertools.product((0.3, 0.5, 0.7), (0.5, 0.7)))
    )
    counts = {pair: [0, 0, 0] for pair in threshold_grid}
    losses = []
    exact = {pair: 0 for pair in threshold_grid}
    sheets = 0
    for images, targets in loader:
        outputs = model(images.to(device))
        loss, _parts = cad_graph_loss(outputs, targets)
        losses.append(float(loss))
        for batch_index, target in enumerate(targets):
            truth = _truth_edges(target)
            sheets += 1
            for pair in threshold_grid:
                predicted = decode_graph_segments(
                    outputs,
                    batch_index,
                    node_threshold=pair[0],
                    edge_threshold=pair[1],
                )
                tp, fp, fn = _counts(predicted, truth)
                counts[pair][0] += tp
                counts[pair][1] += fp
                counts[pair][2] += fn
                exact[pair] += int(fp == 0 and fn == 0)
    candidates = []
    for pair, (tp, fp, fn) in counts.items():
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = 2 * precision * recall / max(precision + recall, 1e-12)
        candidates.append(
            {
                "node_threshold": pair[0],
                "edge_threshold": pair[1],
                "matched": tp,
                "false_positive": fp,
                "false_negative": fn,
                "entity_precision": precision,
                "entity_recall": recall,
                "entity_f1": f1,
                "exact_sheet_rate": exact[pair] / max(sheets, 1),
            }
        )
    candidates.sort(
        key=lambda row: (
            row["entity_f1"],
            row["exact_sheet_rate"],
            row["entity_precision"],
        ),
        reverse=True,
    )
    model.train()
    return {
        "loss": sum(losses) / max(len(losses), 1),
        **candidates[0],
        "threshold_candidates": candidates if thresholds is None else None,
    }


def _warm_start(model: CadGraphModel, path: pathlib.Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    state = torch.load(path, map_location="cpu")
    own = model.state_dict()
    compatible = {
        key: value
        for key, value in state["model"].items()
        if key in own and own[key].shape == value.shape
        and (
            key.startswith("encoder.")
            or key.startswith("decoder.")
            or key.startswith("query_embed.")
        )
    }
    model.load_state_dict(compatible, strict=False)
    return state.get("step")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--warm-start", type=pathlib.Path)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--eval-every", type=int, default=250)
    parser.add_argument("--num-workers", type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    datasets = {
        split: GraphDataset(args.data / f"{split}.jsonl")
        for split in ("train", "val", "holdout")
    }
    loaders = {
        split: DataLoader(
            dataset,
            batch_size=args.batch_size,
            shuffle=split == "train",
            collate_fn=collate_graphs,
            num_workers=args.num_workers,
            pin_memory=device.type == "cuda",
        )
        for split, dataset in datasets.items()
    }
    model = CadGraphModel()
    warm_start_step = _warm_start(model, args.warm_start)
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    best_f1 = -1.0
    history = []
    step = 0
    started = time.monotonic()
    for epoch in range(args.epochs):
        for images, targets in loaders["train"]:
            outputs = model(images.to(device, non_blocking=True))
            loss, parts = cad_graph_loss(outputs, targets)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            step += 1
            if step % 50 == 0:
                values = " ".join(f"{name}={value:.4f}" for name, value in parts.items())
                print(
                    f"epoch={epoch} step={step} loss={float(loss.detach()):.4f} "
                    f"{values} elapsed={time.monotonic() - started:.0f}s",
                    flush=True,
                )
            if step % args.eval_every != 0:
                continue
            validation = evaluate(model, loaders["val"], device)
            history.append({"step": step, "epoch": epoch, "validation": validation})
            checkpoint = {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "step": step,
                "epoch": epoch,
                "validation": validation,
                "architecture": "cad-graph-v1",
                "selection_metric": "entity_f1",
                "selection_split": "source_grouped_val",
                "warm_start_primitive_step": warm_start_step,
            }
            torch.save(checkpoint, args.out / f"ckpt_step{step}.pt")
            if validation["entity_f1"] > best_f1:
                best_f1 = validation["entity_f1"]
                torch.save(checkpoint, args.out / "best.pt")
            print(
                f"validation_entity={validation['entity_precision']:.6f}/"
                f"{validation['entity_recall']:.6f}/"
                f"{validation['entity_f1']:.6f} exact={validation['exact_sheet_rate']:.6f} "
                f"thresholds={validation['node_threshold']}/{validation['edge_threshold']} "
                f"best_f1={best_f1:.6f}",
                flush=True,
            )
    if not history:
        raise RuntimeError("no validation checkpoint produced")
    best = torch.load(args.out / "best.pt", map_location=device)
    model.load_state_dict(best["model"])
    validation = best["validation"]
    holdout = evaluate(
        model,
        loaders["holdout"],
        device,
        thresholds=(validation["node_threshold"], validation["edge_threshold"]),
    )
    report = {
        "architecture": "cad-graph-v1",
        "selection_metric": "entity_f1",
        "selection_split": "source_grouped_val",
        "best_step": best["step"],
        "warm_start_primitive_step": warm_start_step,
        "best_validation": validation,
        "history": history,
        "independent_real_holdout": holdout,
    }
    (args.out / "metrics.json").write_text(json.dumps(report, indent=2))
    print(f"independent_real_holdout={holdout}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
