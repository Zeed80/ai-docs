#!/usr/bin/env python3
"""B7: fine-tune the vendored Deep-Vectorization line model on OUR patches.

A deliberately small training loop instead of the vendored
train_vectorization.py: that script hard-requires the SESYD synthetic corpus
layout; ours loads a single PreprocessedDataset directory produced by
make_dataset.py and fine-tunes FROM the published checkpoint with a low LR.

Loss follows the model's output convention (rows of (x1,y1,x2,y2,w)/64 +
confidence): Hungarian-free ordered matching is avoided by greedy
nearest-target assignment per prediction — adequate for fine-tuning where
the base model already emits sensibly ordered rows.

Runs INSIDE the technical-vectorizer container (torch 1.7.1 matches the
checkpoint; CUDA is unavailable there for RTX 3090 → CPU, so keep epochs
small and rely on the eval gate):

  docker cp tools/vectorizer-finetune/finetune.py infra-technical-vectorizer-1:/tmp/
  docker exec infra-technical-vectorizer-1 python /tmp/finetune.py \
      --data /tmp/ours --out /tmp/model_lines_ft.weights --max-batches 300
"""

from __future__ import annotations

import argparse
import pathlib
import pickle
import sys
import time

import numpy as np
import torch

sys.path.insert(0, "/app/vendor")

from vectorization import load_model  # noqa: E402

SPEC = ("/app/vendor/vectorization/models/specs/"
        "resnet18_blocks3_bn_256__c2h__trans_heads4_feat256_blocks4_ffmaps512__h2o__out512.json")
MAX_LINES = 10


def load_dataset(data_dir: pathlib.Path):
    meta = pickle.load(open(data_dir / "meta.pkl", "rb"))
    n = meta["samples_n"]
    images = np.fromfile(data_dir / "images.bin", dtype="<f4").reshape(n, 3, meta["patch_height"], meta["patch_width"])
    targets = np.fromfile(data_dir / "targets.bin", dtype="<f4").reshape(n, *meta["target_shape"])
    return torch.from_numpy(images), torch.from_numpy(targets)


def _serialize_state_dict(checkpoint: dict) -> dict:
    state = checkpoint["model_state_dict"]
    for k in [k for k in state if "hidden.transformer" in k]:
        state["hidden.decoder.transformer" + k[len("hidden.transformer"):]] = state.pop(k)
    return checkpoint


def matched_loss(pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    """pred/target: [B,10,6]. Greedy per-prediction nearest REAL target for
    the coordinate loss; BCE on the confidence column against 'this row has a
    matched real line'."""
    coords_p, conf_p = pred[..., :5], pred[..., 5].clamp(1e-4, 1 - 1e-4)
    coords_t, present = target[..., :5], target[..., 5]
    # pairwise endpoint distance [B,10p,10t]
    d = (coords_p[:, :, None, :4] - coords_t[:, None, :, :4]).abs().sum(-1)
    # also try the swapped endpoint order — a line has no direction
    swapped = coords_t[..., [2, 3, 0, 1]]
    d2 = (coords_p[:, :, None, :4] - swapped[:, None, :, :4]).abs().sum(-1)
    d = torch.min(d, d2)
    big = torch.full_like(d, 1e6)
    d = torch.where(present[:, None, :] > 0, d, big)
    best, idx = d.min(dim=2)  # [B,10p]
    has_any = present.sum(dim=1, keepdim=True) > 0
    matched = (best < 0.6) & has_any  # within ~38px of some real line
    gathered = torch.gather(coords_t, 1, idx[..., None].expand(-1, -1, 5))
    l1 = (coords_p - gathered).abs().sum(-1)
    coord_loss = (l1 * matched.float()).sum() / matched.float().sum().clamp(min=1)
    conf_loss = torch.nn.functional.binary_cross_entropy(conf_p, matched.float())
    return coord_loss + conf_loss


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--init", default="/models/model_lines.weights")
    parser.add_argument("--out", required=True)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--max-batches", type=int, default=0, help="0 = full epoch")
    parser.add_argument("--val-fraction", type=float, default=0.1)
    args = parser.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    images, targets = load_dataset(pathlib.Path(args.data))
    n_val = max(int(images.shape[0] * args.val_fraction), 1)
    train_x, val_x = images[:-n_val], images[-n_val:]
    train_y, val_y = targets[:-n_val], targets[-n_val:]
    print(f"dataset: train {train_x.shape[0]}, val {val_x.shape[0]}, device {device}")

    model = load_model(SPEC).to(device)
    checkpoint = _serialize_state_dict(torch.load(args.init, map_location=device))
    model.load_state_dict(checkpoint["model_state_dict"])

    def evaluate() -> float:
        model.eval()
        losses = []
        with torch.no_grad():
            for start in range(0, val_x.shape[0], args.batch_size):
                xb = val_x[start:start + args.batch_size].to(device)
                yb = val_y[start:start + args.batch_size].to(device)
                losses.append(matched_loss(model(xb, MAX_LINES), yb).item())
        model.train()
        return float(np.mean(losses))

    base_val = evaluate()
    print(f"val loss BEFORE fine-tune: {base_val:.4f}")

    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr)
    model.train()
    step = 0
    for epoch in range(args.epochs):
        order = torch.randperm(train_x.shape[0])
        for start in range(0, train_x.shape[0], args.batch_size):
            idx = order[start:start + args.batch_size]
            xb, yb = train_x[idx].to(device), train_y[idx].to(device)
            optimizer.zero_grad()
            loss = matched_loss(model(xb, MAX_LINES), yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            step += 1
            if step % 20 == 0:
                print(f"epoch {epoch} step {step}: loss {loss.item():.4f} ({time.strftime('%H:%M:%S')})")
            if args.max_batches and step >= args.max_batches:
                break
        if args.max_batches and step >= args.max_batches:
            break

    final_val = evaluate()
    print(f"val loss AFTER fine-tune: {final_val:.4f} (before {base_val:.4f})")
    torch.save({"model_state_dict": model.state_dict()}, args.out)
    print(f"saved: {args.out}")
    # honest verdict for the caller: did fine-tuning move the needle at all?
    return 0 if final_val < base_val else 2


if __name__ == "__main__":
    sys.exit(main())
