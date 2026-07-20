#!/usr/bin/env python3
"""Train directional geometry fields with a source-grouped selection split."""

from __future__ import annotations

import argparse
import json
import pathlib
import time

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from directional_dataset import HEATMAP_NAMES, DirectionalFieldDataset
from directional_loss import directional_loss
from directional_model import DirectionalFieldModel


@torch.no_grad()
def evaluate(model, loader, device, threshold: float = 0.5) -> dict[str, float]:
    model.eval()
    losses: list[float] = []
    tp = torch.zeros(len(HEATMAP_NAMES), device=device)
    fp = torch.zeros_like(tp)
    fn = torch.zeros_like(tp)
    direction_sum = torch.tensor(0.0, device=device)
    direction_pixels = torch.tensor(0.0, device=device)
    radius_error = torch.tensor(0.0, device=device)
    radius_pixels = torch.tensor(0.0, device=device)
    for images, targets in loader:
        targets = targets.to(device)
        output = model(images.to(device))
        loss, _parts = directional_loss(output, targets)
        losses.append(float(loss))
        predicted = output[:, : len(HEATMAP_NAMES)].sigmoid() >= threshold
        expected = targets[:, : len(HEATMAP_NAMES)] >= 0.5
        tp += (predicted & expected).sum(dim=(0, 2, 3))
        fp += (predicted & ~expected).sum(dim=(0, 2, 3))
        fn += (~predicted & expected).sum(dim=(0, 2, 3))
        line_mask = targets[:, 0:1]
        predicted_direction = F.normalize(output[:, 6:8], dim=1, eps=1e-6)
        direction_sum += (
            (predicted_direction * targets[:, 6:8]).sum(dim=1, keepdim=True)
            * line_mask
        ).sum()
        direction_pixels += line_mask.sum()
        center_mask = targets[:, 5:6]
        radius_error += (
            (output[:, 8:9].sigmoid() - targets[:, 8:9]).abs() * center_mask
        ).sum()
        radius_pixels += center_mask.sum()
    precision = tp / (tp + fp).clamp(min=1)
    recall = tp / (tp + fn).clamp(min=1)
    f1 = 2 * precision * recall / (precision + recall).clamp(min=1e-12)
    metrics = {
        "loss": sum(losses) / max(len(losses), 1),
        "macro_heatmap_f1": float(f1.mean()),
        "direction_cosine": float(direction_sum / direction_pixels.clamp(min=1)),
        "radius_mae": float(radius_error / radius_pixels.clamp(min=1)),
    }
    for index, name in enumerate(HEATMAP_NAMES):
        metrics[f"{name}_precision"] = float(precision[index])
        metrics[f"{name}_recall"] = float(recall[index])
        metrics[f"{name}_f1"] = float(f1[index])
    # Selection remains synthetic/source-grouped.  Endpoint and junction
    # localization carry more weight than dense line pixels.
    metrics["selection_score"] = (
        0.25 * metrics["line_f1"]
        + 0.25 * metrics["endpoint_f1"]
        + 0.2 * metrics["junction_f1"]
        + 0.1 * metrics["circle_f1"]
        + 0.1 * metrics["arc_f1"]
        + 0.1 * metrics["center_f1"]
    )
    model.train()
    return metrics


def _warm_start(model: DirectionalFieldModel, checkpoint: pathlib.Path) -> int | None:
    if not checkpoint.exists():
        return None
    state = torch.load(checkpoint, map_location="cpu")
    compatible = {
        key: value
        for key, value in state["model"].items()
        if not key.startswith("head.")
    }
    model.load_state_dict(compatible, strict=False)
    return state.get("step")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--warm-start", type=pathlib.Path)
    parser.add_argument("--epochs", type=int, default=18)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--eval-every", type=int, default=200)
    parser.add_argument("--num-workers", type=int, default=0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    datasets = {
        split: DirectionalFieldDataset(args.data / f"{split}.jsonl")
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
    model = DirectionalFieldModel()
    warm_start_step = _warm_start(model, args.warm_start) if args.warm_start else None
    model.to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    best_score = -1.0
    history: list[dict] = []
    step = 0
    started = time.monotonic()
    for epoch in range(args.epochs):
        for images, targets in loaders["train"]:
            output = model(images.to(device, non_blocking=True))
            loss, parts = directional_loss(
                output,
                targets.to(device, non_blocking=True),
            )
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            step += 1
            if step % 50 == 0:
                values = " ".join(f"{name}={float(value):.4f}" for name, value in parts.items())
                print(
                    f"epoch={epoch} step={step} loss={float(loss.detach()):.4f} "
                    f"{values} elapsed={time.monotonic() - started:.0f}s",
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
                    "architecture": "directional-fields-v1",
                    "selection_split": "source_grouped_val",
                    "warm_start_evidence_step": warm_start_step,
                }
                torch.save(checkpoint, args.out / f"ckpt_step{step}.pt")
                if validation["selection_score"] > best_score:
                    best_score = validation["selection_score"]
                    torch.save(checkpoint, args.out / "best.pt")
                print(
                    f"validation={validation} best_selection_score={best_score:.6f}",
                    flush=True,
                )
    if not history:
        raise RuntimeError("no validation checkpoint was produced; lower --eval-every")
    best = torch.load(args.out / "best.pt", map_location=device)
    model.load_state_dict(best["model"])
    holdout = evaluate(model, loaders["holdout"], device)
    report = {
        "architecture": "directional-fields-v1",
        "selection_metric": "weighted_heatmap_f1",
        "selection_split": "source_grouped_val",
        "best_validation_score": best_score,
        "best_step": best["step"],
        "warm_start_evidence_step": warm_start_step,
        "history": history,
        "independent_real_holdout": holdout,
    }
    (args.out / "metrics.json").write_text(json.dumps(report, indent=2))
    print(f"independent_real_holdout={holdout}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
