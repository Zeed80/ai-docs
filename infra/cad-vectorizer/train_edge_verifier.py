#!/usr/bin/env python3
"""Train a line-of-interest edge classifier over frozen directional fields."""

from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import time

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

from directional_model import DirectionalFieldModel
from edge_verifier import EdgeVerifier, decode_verified_edges, pair_features
from graph_dataset import GraphDataset, collate_graphs
from train_graph import _counts, _truth_edges


@torch.no_grad()
def build_pair_corpus(field_model, loader, device, negative_ratio: int = 4):
    feature_rows = []
    label_rows = []
    field_model.eval()
    for images, targets in loader:
        outputs = field_model(images.to(device))
        for batch_index, target in enumerate(targets):
            coords = target["coords"].to(device)
            if len(coords) < 2:
                continue
            indices = torch.combinations(torch.arange(len(coords), device=device), r=2)
            pairs = torch.cat((coords[indices[:, 0]], coords[indices[:, 1]]), dim=-1)
            features = pair_features(outputs[batch_index], pairs)
            adjacency = target["adjacency"].to(device)
            labels = adjacency[indices[:, 0], indices[:, 1]]
            positive = torch.where(labels > 0.5)[0]
            negative = torch.where(labels <= 0.5)[0]
            if len(negative):
                limit = min(len(negative), max(len(positive) * negative_ratio, 16))
                # Hard negatives already look line-like to the dense model.
                hard_order = features[negative, 4].argsort(descending=True)[:limit]
                selected = torch.cat((positive, negative[hard_order]))
            else:
                selected = positive
            if len(selected):
                feature_rows.append(features[selected].cpu())
                label_rows.append(labels[selected].cpu())
    return torch.cat(feature_rows), torch.cat(label_rows)


@torch.no_grad()
def evaluate_entities(
    field_model,
    verifier,
    loader,
    device,
    *,
    thresholds: tuple[float, float] | None = None,
) -> dict:
    field_model.eval()
    verifier.eval()
    grid = (
        [thresholds]
        if thresholds
        else list(itertools.product((0.5, 0.6, 0.7), (0.5, 0.7, 0.9)))
    )
    totals = {pair: [0, 0, 0, 0] for pair in grid}
    sheets = 0
    for images, targets in loader:
        outputs = field_model(images.to(device))
        for batch_index, target in enumerate(targets):
            truth = _truth_edges(target)
            sheets += 1
            for pair in grid:
                predicted = decode_verified_edges(
                    outputs[batch_index],
                    verifier,
                    node_threshold=pair[0],
                    edge_threshold=pair[1],
                )
                tp, fp, fn = _counts(predicted, truth)
                totals[pair][0] += tp
                totals[pair][1] += fp
                totals[pair][2] += fn
                totals[pair][3] += int(fp == 0 and fn == 0)
    candidates = []
    for pair, (tp, fp, fn, exact) in totals.items():
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
                "exact_sheet_rate": exact / max(sheets, 1),
            }
        )
    candidates.sort(
        key=lambda row: (row["entity_f1"], row["exact_sheet_rate"], row["entity_precision"]),
        reverse=True,
    )
    return {**candidates[0], "threshold_candidates": candidates if not thresholds else None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--directional-checkpoint", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=512)
    parser.add_argument("--lr", type=float, default=3e-4)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    directional_state = torch.load(args.directional_checkpoint, map_location=device)
    field_model = DirectionalFieldModel().to(device)
    field_model.load_state_dict(directional_state["model"])
    field_model.eval()
    datasets = {
        split: GraphDataset(args.data / f"{split}.jsonl")
        for split in ("train", "val", "holdout")
    }
    graph_loaders = {
        split: DataLoader(
            dataset,
            batch_size=8,
            shuffle=False,
            collate_fn=collate_graphs,
        )
        for split, dataset in datasets.items()
    }
    started = time.monotonic()
    train_features, train_labels = build_pair_corpus(
        field_model, graph_loaders["train"], device
    )
    val_features, val_labels = build_pair_corpus(
        field_model, graph_loaders["val"], device
    )
    print(
        f"pair_corpus train={len(train_labels)} val={len(val_labels)} "
        f"positive={int(train_labels.sum())} elapsed={time.monotonic() - started:.0f}s",
        flush=True,
    )
    train_loader = DataLoader(
        TensorDataset(train_features, train_labels),
        batch_size=args.batch_size,
        shuffle=True,
        pin_memory=device.type == "cuda",
    )
    verifier = EdgeVerifier().to(device)
    optimizer = torch.optim.AdamW(verifier.parameters(), lr=args.lr, weight_decay=1e-4)
    positives = train_labels.sum()
    positive_weight = ((len(train_labels) - positives) / positives.clamp(min=1)).to(device)
    best_f1 = -1.0
    history = []
    for epoch in range(args.epochs):
        verifier.train()
        losses = []
        for features, labels in train_loader:
            logits = verifier(features.to(device, non_blocking=True))
            loss = F.binary_cross_entropy_with_logits(
                logits,
                labels.to(device, non_blocking=True),
                pos_weight=positive_weight,
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        verifier.eval()
        with torch.no_grad():
            val_logits = verifier(val_features.to(device))
            val_loss = float(
                F.binary_cross_entropy_with_logits(
                    val_logits,
                    val_labels.to(device),
                    pos_weight=positive_weight,
                )
            )
        if epoch % 2 == 1 or epoch == args.epochs - 1:
            entity = evaluate_entities(
                field_model,
                verifier,
                graph_loaders["val"],
                device,
            )
            history.append(
                {
                    "epoch": epoch,
                    "train_loss": sum(losses) / max(len(losses), 1),
                    "validation_loss": val_loss,
                    "validation_entity": entity,
                }
            )
            checkpoint = {
                "model": verifier.state_dict(),
                "epoch": epoch,
                "directional_step": directional_state.get("step"),
                "validation_entity": entity,
                "architecture": "dense-edge-verifier-v1",
                "selection_split": "source_grouped_val",
            }
            torch.save(checkpoint, args.out / f"ckpt_epoch{epoch}.pt")
            if entity["entity_f1"] > best_f1:
                best_f1 = entity["entity_f1"]
                torch.save(checkpoint, args.out / "best.pt")
            print(
                f"epoch={epoch} loss={history[-1]['train_loss']:.5f} "
                f"val_loss={val_loss:.5f} entity="
                f"{entity['entity_precision']:.6f}/{entity['entity_recall']:.6f}/"
                f"{entity['entity_f1']:.6f} thresholds="
                f"{entity['node_threshold']}/{entity['edge_threshold']} "
                f"best={best_f1:.6f}",
                flush=True,
            )
    best = torch.load(args.out / "best.pt", map_location=device)
    verifier.load_state_dict(best["model"])
    selected = best["validation_entity"]
    holdout = evaluate_entities(
        field_model,
        verifier,
        graph_loaders["holdout"],
        device,
        thresholds=(selected["node_threshold"], selected["edge_threshold"]),
    )
    report = {
        "architecture": "dense-edge-verifier-v1",
        "selection_metric": "entity_f1",
        "selection_split": "source_grouped_val",
        "directional_step": directional_state.get("step"),
        "best_epoch": best["epoch"],
        "best_validation": selected,
        "history": history,
        "independent_real_holdout": holdout,
    }
    (args.out / "metrics.json").write_text(json.dumps(report, indent=2))
    print(f"independent_real_holdout={holdout}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
