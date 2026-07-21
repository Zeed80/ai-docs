#!/usr/bin/env python3
"""Train and independently evaluate the opt-in multi-type proposal model."""

from __future__ import annotations

import argparse
import json
import pathlib
import time

import torch
from torch.utils.data import DataLoader

from multi_type_dataset import MultiTypeProposalDataset, collate_multi_type
from multi_type_loss import multi_type_loss, proposal_metrics
from multi_type_model import MultiTypeProposalModel


def _merge_metrics(items: list[dict]) -> dict:
    counts: dict[str, dict[str, int]] = {}
    tolerance = 0.01
    for item in items:
        tolerance = item["tolerance"]
        for name, values in item["by_type"].items():
            count = counts.setdefault(name, {"tp": 0, "pred": 0, "target": 0})
            for key in count:
                count[key] += int(values[key])
    by_type = {}
    for name, count in counts.items():
        precision = count["tp"] / max(count["pred"], 1)
        recall = count["tp"] / max(count["target"], 1)
        by_type[name] = {
            **count,
            "precision": precision,
            "recall": recall,
            "f1": 2 * precision * recall / max(precision + recall, 1e-12),
        }
    tp = sum(item["tp"] for item in counts.values())
    predicted = sum(item["pred"] for item in counts.values())
    target = sum(item["target"] for item in counts.values())
    precision = tp / max(predicted, 1)
    recall = tp / max(target, 1)
    return {
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / max(precision + recall, 1e-12),
        "by_type": by_type,
        "tolerance": tolerance,
    }


@torch.no_grad()
def evaluate(model, loader, device) -> dict:
    model.eval()
    losses = []
    metrics = []
    for images, targets in loader:
        outputs = model(images.to(device))
        loss, _ = multi_type_loss(outputs, targets)
        losses.append(float(loss))
        metrics.append(proposal_metrics(outputs, targets))
    result = _merge_metrics(metrics)
    result["loss"] = sum(losses) / max(len(losses), 1)
    model.train()
    return result


def _checkpoint(model, optimizer, step, epoch, metrics, manifests, config):
    return {
        "model": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "step": step,
        "epoch": epoch,
        "validation": metrics,
        "architecture": "multi-type-proposal-v2",
        "target_contract": "cad-ir-proposals-v2",
        "model_config": config,
        "manifests": [str(path) for path in manifests],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", action="append", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--eval-every", type=int, default=500)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--resume", type=pathlib.Path)
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    train_data = MultiTypeProposalDataset(args.manifest, split="train")
    val_data = MultiTypeProposalDataset(args.manifest, split="val")
    if not train_data or not val_data:
        raise SystemExit("manifest must contain non-empty train and val splits")
    train_loader = DataLoader(
        train_data, batch_size=args.batch_size, shuffle=True,
        num_workers=args.num_workers, collate_fn=collate_multi_type,
        pin_memory=device.type == "cuda",
    )
    val_loader = DataLoader(
        val_data, batch_size=args.batch_size, shuffle=False,
        num_workers=args.num_workers, collate_fn=collate_multi_type,
    )
    config = {"d_model": 160, "n_queries": 128, "n_layers": 4, "n_heads": 8, "dim_ff": 640}
    model = MultiTypeProposalModel(**config).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    step = 0
    if args.resume:
        state = torch.load(args.resume, map_location=device)
        if state.get("architecture") != "multi-type-proposal-v2":
            raise SystemExit("resume checkpoint architecture mismatch")
        model.load_state_dict(state["model"])
        optimizer.load_state_dict(state["optimizer"])
        step = int(state.get("step", 0))

    best_f1 = -1.0
    best_loss = float("inf")
    history = []
    started = time.monotonic()

    def validate(epoch: int) -> None:
        nonlocal best_f1, best_loss
        metrics = evaluate(model, val_loader, device)
        history.append({"step": step, "epoch": epoch, "validation": metrics})
        state = _checkpoint(model, optimizer, step, epoch, metrics, args.manifest, config)
        torch.save(state, args.out / f"ckpt_step{step}.pt")
        if metrics["f1"] > best_f1 or (
            metrics["f1"] == best_f1 and metrics["loss"] < best_loss
        ):
            best_f1, best_loss = metrics["f1"], metrics["loss"]
            torch.save(state, args.out / "best.pt")
        (args.out / "metrics.json").write_text(json.dumps({
            "architecture": "multi-type-proposal-v2",
            "selection_split": "source_grouped_val",
            "best_validation_f1": best_f1,
            "best_validation_loss": best_loss,
            "history": history,
        }, indent=2))
        print(f"validation={metrics} best_f1={best_f1:.6f}", flush=True)

    model.train()
    last_validation_step = -1
    for epoch in range(args.epochs):
        for images, targets in train_loader:
            outputs = model(images.to(device, non_blocking=True))
            loss, parts = multi_type_loss(outputs, targets)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            step += 1
            if step % 50 == 0:
                print(f"epoch={epoch} step={step} loss={float(loss.detach()):.4f} parts={parts} elapsed={time.monotonic() - started:.0f}s", flush=True)
            if step % args.eval_every == 0:
                validate(epoch)
                last_validation_step = step
    if last_validation_step != step:
        validate(max(args.epochs - 1, 0))
    print(f"training done: device={device} steps={step} best_f1={best_f1:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
