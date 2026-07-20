#!/usr/bin/env python3
"""Train the unordered primitive-set detector.

Checkpoint selection uses only a source-grouped validation split. Independent
real raster/DWG holdouts remain untouched until the external evaluator/gate.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import time

import torch
from torch.utils.data import DataLoader

from primitive_dataset import PrimitiveSetDataset, collate_primitive_sets
from primitive_loss import primitive_set_loss
from primitive_model import PrimitiveSetModel


@torch.no_grad()
def evaluate(model, loader, device) -> dict[str, float]:
    model.eval()
    losses = []
    matched = []
    target_count = []
    for images, targets in loader:
        images = images.to(device)
        outputs = model(images)
        loss, parts = primitive_set_loss(outputs, targets)
        losses.append(float(loss))
        matched.append(parts["matched"])
        target_count.append(sum(target["types"].numel() for target in targets))
    model.train()
    return {
        "loss": sum(losses) / max(len(losses), 1),
        "matched_fraction": sum(matched) / max(sum(target_count), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--eval-every", type=int, default=200)
    # Zero is the portable/reproducible default. Restricted CI environments
    # often forbid the multiprocessing resource-sharer socket.
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--resume", type=pathlib.Path)
    parser.add_argument(
        "--dataset-kind",
        default="source_grouped_cad",
        help="provenance label stored in checkpoints and metrics",
    )
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    train_data = PrimitiveSetDataset(args.data / "train.jsonl")
    val_data = PrimitiveSetDataset(args.data / "val.jsonl")
    train_loader = DataLoader(
        train_data,
        batch_size=args.batch_size,
        shuffle=True,
        num_workers=args.num_workers,
        collate_fn=collate_primitive_sets,
        pin_memory=device.type == "cuda",
    )
    val_loader = DataLoader(
        val_data,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        collate_fn=collate_primitive_sets,
    )
    model = PrimitiveSetModel().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    step = 0
    if args.resume:
        state = torch.load(args.resume, map_location=device)
        model.load_state_dict(state["model"])
        if "optimizer" in state:
            optimizer.load_state_dict(state["optimizer"])
        step = int(state.get("step", 0))

    best_val = float("inf")
    history = []
    started = time.monotonic()
    model.train()
    for epoch in range(args.epochs):
        for images, targets in train_loader:
            images = images.to(device, non_blocking=True)
            outputs = model(images)
            loss, parts = primitive_set_loss(outputs, targets)
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
                metrics = evaluate(model, val_loader, device)
                history.append({"step": step, "epoch": epoch, "validation": metrics})
                checkpoint = {
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "step": step,
                    "epoch": epoch,
                    "validation": metrics,
                    "architecture": "primitive-set-v1",
                    "dataset_kind": args.dataset_kind,
                }
                path = args.out / f"ckpt_step{step}.pt"
                torch.save(checkpoint, path)
                if metrics["loss"] < best_val:
                    best_val = metrics["loss"]
                    torch.save(checkpoint, args.out / "best.pt")
                (args.out / "metrics.json").write_text(
                    json.dumps(
                        {
                            "architecture": "primitive-set-v1",
                            "selection_split": "source_grouped_val",
                            "dataset_kind": args.dataset_kind,
                            "best_validation_loss": best_val,
                            "history": history,
                        },
                        indent=2,
                    )
                )
                print(f"validation={metrics} best_loss={best_val:.5f}", flush=True)

    if not history:
        metrics = evaluate(model, val_loader, device)
        checkpoint = {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "step": step,
            "epoch": args.epochs - 1,
            "validation": metrics,
            "architecture": "primitive-set-v1",
            "dataset_kind": args.dataset_kind,
        }
        torch.save(checkpoint, args.out / "best.pt")
    print(f"training done: steps={step} best_validation_loss={best_val:.5f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
