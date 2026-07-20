#!/usr/bin/env python3
"""Train raster evidence and select only by geometric validation F1."""

from __future__ import annotations

import argparse
import json
import pathlib
import time

import torch
from torch.utils.data import DataLoader

from evidence_dataset import EVIDENCE_NAMES, EvidenceDataset
from evidence_loss import evidence_loss
from evidence_model import EvidenceHeatmapModel


@torch.no_grad()
def evaluate(model, loader, device, threshold: float = 0.5) -> dict[str, float]:
    model.eval()
    losses = []
    tp = torch.zeros(len(EVIDENCE_NAMES), device=device)
    fp = torch.zeros_like(tp)
    fn = torch.zeros_like(tp)
    for images, targets in loader:
        targets = targets.to(device)
        logits = model(images.to(device))
        losses.append(float(evidence_loss(logits, targets)))
        predicted = logits.sigmoid() >= threshold
        expected = targets >= 0.5
        tp += (predicted & expected).sum(dim=(0, 2, 3))
        fp += (predicted & ~expected).sum(dim=(0, 2, 3))
        fn += (~predicted & expected).sum(dim=(0, 2, 3))
    precision = tp / (tp + fp).clamp(min=1)
    recall = tp / (tp + fn).clamp(min=1)
    f1 = 2 * precision * recall / (precision + recall).clamp(min=1e-12)
    metrics = {
        "loss": sum(losses) / max(len(losses), 1),
        "macro_f1": float(f1.mean()),
    }
    for index, name in enumerate(EVIDENCE_NAMES):
        metrics[f"{name}_precision"] = float(precision[index])
        metrics[f"{name}_recall"] = float(recall[index])
        metrics[f"{name}_f1"] = float(f1[index])
    model.train()
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--eval-every", type=int, default=200)
    parser.add_argument("--num-workers", type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    datasets = {
        split: EvidenceDataset(args.data / f"{split}.jsonl")
        for split in ("train", "val", "holdout")
    }
    loaders = {
        split: DataLoader(
            dataset,
            batch_size=args.batch_size,
            shuffle=split == "train",
            num_workers=args.num_workers,
            pin_memory=device.type == "cuda",
        )
        for split, dataset in datasets.items()
    }
    model = EvidenceHeatmapModel().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    best_score = -1.0
    history = []
    step = 0
    started = time.monotonic()
    for epoch in range(args.epochs):
        for images, targets in loaders["train"]:
            logits = model(images.to(device, non_blocking=True))
            loss = evidence_loss(logits, targets.to(device, non_blocking=True))
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            step += 1
            if step % 50 == 0:
                print(
                    f"epoch={epoch} step={step} loss={float(loss.detach()):.4f} "
                    f"elapsed={time.monotonic() - started:.0f}s",
                    flush=True,
                )
            if step % args.eval_every == 0:
                validation = evaluate(model, loaders["val"], device)
                history.append({"step": step, "epoch": epoch, "validation": validation})
                checkpoint = {
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "step": step,
                    "epoch": epoch,
                    "validation": validation,
                    "architecture": "evidence-heatmap-v1",
                    "selection_split": "source_grouped_val",
                }
                torch.save(checkpoint, args.out / f"ckpt_step{step}.pt")
                if validation["macro_f1"] > best_score:
                    best_score = validation["macro_f1"]
                    torch.save(checkpoint, args.out / "best.pt")
                print(
                    f"validation={validation} best_macro_f1={best_score:.6f}",
                    flush=True,
                )
    if not history:
        validation = evaluate(model, loaders["val"], device)
        checkpoint = {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "step": step,
            "epoch": args.epochs - 1,
            "validation": validation,
            "architecture": "evidence-heatmap-v1",
            "selection_split": "source_grouped_val",
        }
        torch.save(checkpoint, args.out / "best.pt")
        best_score = validation["macro_f1"]
    best = torch.load(args.out / "best.pt", map_location=device)
    model.load_state_dict(best["model"])
    holdout = evaluate(model, loaders["holdout"], device)
    (args.out / "metrics.json").write_text(
        json.dumps(
            {
                "architecture": "evidence-heatmap-v1",
                "selection_metric": "macro_f1",
                "selection_split": "source_grouped_val",
                "best_validation_macro_f1": best_score,
                "best_step": best["step"],
                "history": history,
                "independent_real_holdout": holdout,
            },
            indent=2,
        )
    )
    print(f"independent_real_holdout={holdout}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
