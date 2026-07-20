#!/usr/bin/env python3
"""Train global orthographic-view layout recognition before local geometry."""

from __future__ import annotations

import argparse
import json
import pathlib
import time

import torch
from scipy.optimize import linear_sum_assignment
from torch.utils.data import DataLoader

from sheet_layout_dataset import SheetLayoutDataset, collate_sheet_layout
from sheet_layout_loss import pairwise_iou, sheet_layout_loss
from sheet_layout_model import SheetLayoutModel


@torch.no_grad()
def evaluate(model, loader, device, *, confidence: float = 0.5) -> dict[str, float]:
    model.eval()
    losses = []
    true_positive = false_positive = false_negative = exact_sheets = sheets = 0
    matched_ious: list[float] = []
    for images, targets in loader:
        outputs = model(images.to(device))
        loss, _parts = sheet_layout_loss(outputs, targets)
        losses.append(float(loss))
        probabilities = outputs["type_logits"].softmax(-1)
        scores, kinds = probabilities.max(-1)
        for batch_index, target in enumerate(targets):
            sheets += 1
            keep = (kinds[batch_index] != 0) & (scores[batch_index] >= confidence)
            predicted_types = kinds[batch_index][keep]
            predicted_boxes = outputs["boxes"][batch_index][keep]
            target_types = target["types"].to(device)
            target_boxes = target["boxes"].to(device)
            matches = 0
            if predicted_boxes.numel() and target_boxes.numel():
                ious = pairwise_iou(predicted_boxes, target_boxes)
                same_type = predicted_types[:, None] == target_types[None, :]
                valid = same_type & (ious >= 0.5)
                cost = torch.where(valid, 1.0 - ious, torch.full_like(ious, 1e3))
                rows, columns = linear_sum_assignment(cost.detach().cpu().numpy())
                for row, column in zip(rows, columns, strict=True):
                    if bool(valid[row, column]):
                        matches += 1
                        matched_ious.append(float(ious[row, column]))
            predicted_count = int(predicted_types.numel())
            target_count = int(target_types.numel())
            true_positive += matches
            false_positive += predicted_count - matches
            false_negative += target_count - matches
            exact_sheets += int(matches == predicted_count == target_count)
    precision = true_positive / max(true_positive + false_positive, 1)
    recall = true_positive / max(true_positive + false_negative, 1)
    model.train()
    return {
        "loss": sum(losses) / max(len(losses), 1),
        "view_precision_iou50": precision,
        "view_recall_iou50": recall,
        "view_f1_iou50": 2 * precision * recall / max(precision + recall, 1e-12),
        "mean_matched_iou": sum(matched_ious) / max(len(matched_ious), 1),
        "exact_sheet_rate": exact_sheets / max(sheets, 1),
        "sheets": float(sheets),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--eval-every", type=int, default=100)
    parser.add_argument("--num-workers", type=int, default=0)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.data / "manifest.jsonl"
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    datasets = {
        split: SheetLayoutDataset(manifest, split)
        for split in ("train", "val", "holdout")
    }
    loaders = {
        split: DataLoader(
            dataset,
            batch_size=args.batch_size,
            shuffle=split == "train",
            num_workers=args.num_workers,
            collate_fn=collate_sheet_layout,
            pin_memory=device.type == "cuda",
        )
        for split, dataset in datasets.items()
    }
    if not datasets["train"] or not datasets["val"]:
        raise SystemExit("train and validation splits must be non-empty")
    model = SheetLayoutModel().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    best_score = (-1.0, -1.0, -1.0)
    history = []
    step = 0
    started = time.monotonic()
    for epoch in range(args.epochs):
        model.train()
        for images, targets in loaders["train"]:
            outputs = model(images.to(device, non_blocking=True))
            loss, parts = sheet_layout_loss(outputs, targets)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            step += 1
            if step % 50 == 0:
                print(
                    f"epoch={epoch} step={step} loss={float(loss.detach()):.4f} "
                    f"parts={parts} elapsed={time.monotonic() - started:.0f}s",
                    flush=True,
                )
            if step % args.eval_every == 0:
                validation = evaluate(model, loaders["val"], device)
                record = {"step": step, "epoch": epoch, "validation": validation}
                history.append(record)
                checkpoint = {
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "step": step,
                    "epoch": epoch,
                    "validation": validation,
                    "architecture": "sheet-layout-v1",
                    "selection_split": "source_grouped_val",
                }
                torch.save(checkpoint, args.out / f"ckpt_step{step}.pt")
                selection_score = (
                    validation["view_f1_iou50"],
                    validation["exact_sheet_rate"],
                    validation["mean_matched_iou"],
                )
                if selection_score > best_score:
                    best_score = selection_score
                    torch.save(checkpoint, args.out / "best.pt")
                print(
                    f"validation={validation} best_selection={best_score}",
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
            "architecture": "sheet-layout-v1",
            "selection_split": "source_grouped_val",
        }
        torch.save(checkpoint, args.out / "best.pt")
        best_score = (
            validation["view_f1_iou50"],
            validation["exact_sheet_rate"],
            validation["mean_matched_iou"],
        )
    # The reserved web holdout is opened once, after checkpoint selection.
    best_state = torch.load(args.out / "best.pt", map_location=device)
    model.load_state_dict(best_state["model"])
    holdout = evaluate(model, loaders["holdout"], device)
    metrics = {
        "architecture": "sheet-layout-v1",
        "selection_split": "source_grouped_val",
        "selection_metric": "view_f1_iou50, exact_sheet_rate, mean_matched_iou",
        "best_validation_score": list(best_score),
        "best_step": best_state["step"],
        "history": history,
        "reserved_web_holdout": holdout,
    }
    (args.out / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(f"reserved_web_holdout={holdout}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
