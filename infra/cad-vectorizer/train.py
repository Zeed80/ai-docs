#!/usr/bin/env python3
"""Train the CAD IR neural vectorizer.

Loss: cross-entropy on command/line_class/width_class at every valid row;
masked L1 on the continuous params (masked both by padding AND by the
sequence encoding's own "unused slot" sentinel, -1 — e.g. a CIRCLE row only
supervises params[0:3], not the two slots a SEGMENT would use).

Checkpoints + a JSON metrics file land in --out every --eval-every steps,
scored by HOLDOUT coverage (recall/precision of the greedy-decoded, re-
rendered sequence against the real-DWG ground truth ink) — the exact same
metric family ``eval_vectorize.py`` uses for the CV baseline, so Ф3.5's
"beat the baseline" comparison is apples-to-apples. Best-holdout checkpoint
is tracked explicitly: per the project's LoRA training experience, the loss
curve keeps improving past the point where holdout quality peaks and then
regresses (overfitting on a fixed synthetic distribution) — selecting by
holdout, not by last-checkpoint or by train loss, is the whole point.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from dataset import CadSequenceDataset, collate
from model import IMG_SIZE, N_PARAMS, CadVectorizerModel

_UNUSED = -1.0


def _light_backend_imports(repo_backend: pathlib.Path):
    """Import ``app.ai.cad_ir``/``cad_recognize`` WITHOUT running the full
    ``app.ai`` package ``__init__`` (which pulls in the AI router, every LLM
    provider, ``app.config`` — a whole production backend's worth of deps
    this standalone trainer venv has no reason to carry). Pre-registering
    empty stand-in modules at ``app``/``app.ai`` makes Python's import
    machinery treat them as already-imported and skip their real files;
    the cad_ir/cad_recognize submodules only import each other and
    structlog/pydantic/cv2/shapely, so nothing else is pulled in.
    """
    import types

    sys.path.insert(0, str(repo_backend))
    if "app" not in sys.modules:
        pkg = types.ModuleType("app")
        pkg.__path__ = [str(repo_backend / "app")]
        sys.modules["app"] = pkg
    if "app.ai" not in sys.modules:
        pkg = types.ModuleType("app.ai")
        pkg.__path__ = [str(repo_backend / "app" / "ai")]
        sys.modules["app.ai"] = pkg

    from app.ai.cad_ir import CadIR
    from app.ai.cad_ir.schema import SourceInfo
    from app.ai.cad_ir.sequence import decode
    from app.ai.cad_recognize.verify import score_coverage

    return CadIR, SourceInfo, decode, score_coverage


def compute_loss(cmd_logits, param_pred, lc_logits, wc_logits, targets, pad_mask):
    cmd_tgt, params_tgt, lc_tgt, wc_tgt = targets
    valid = pad_mask.reshape(-1)
    n_valid = valid.sum().clamp(min=1)

    cmd_loss = F.cross_entropy(cmd_logits.reshape(-1, cmd_logits.size(-1)), cmd_tgt.reshape(-1), reduction="none")
    cmd_loss = (cmd_loss * valid).sum() / n_valid

    lc_loss = F.cross_entropy(lc_logits.reshape(-1, lc_logits.size(-1)), lc_tgt.reshape(-1), reduction="none")
    lc_loss = (lc_loss * valid).sum() / n_valid
    wc_loss = F.cross_entropy(wc_logits.reshape(-1, wc_logits.size(-1)), wc_tgt.reshape(-1), reduction="none")
    wc_loss = (wc_loss * valid).sum() / n_valid

    param_mask = (params_tgt != _UNUSED) & pad_mask.unsqueeze(-1)
    diff = (param_pred - torch.nan_to_num(params_tgt, nan=0.0)).abs()
    n_param_valid = param_mask.sum().clamp(min=1)
    param_loss = (diff * param_mask).sum() / n_param_valid

    total = cmd_loss + lc_loss + wc_loss + 2.0 * param_loss  # params get more weight — geometry is the point
    return total, {
        "cmd": cmd_loss.item(), "lc": lc_loss.item(), "wc": wc_loss.item(), "param": param_loss.item(),
    }


@torch.no_grad()
def eval_holdout(model, holdout_manifest: pathlib.Path, repo_backend: pathlib.Path, device, max_samples: int = 20):
    """Greedy-decode each holdout image, rebuild IR entities, coverage-score
    against the real-DWG ground truth ink. Returns mean recall/precision."""
    import numpy as np
    from PIL import Image

    CadIR, SourceInfo, decode, score_coverage = _light_backend_imports(repo_backend)

    rows = [json.loads(line) for line in open(holdout_manifest)][:max_samples]
    if not rows:
        return None
    recalls, precisions = [], []
    for row in rows:
        ir = CadIR.model_validate_json(pathlib.Path(row["ir"]).read_text())
        img = Image.open(row["image"]).convert("L")
        w0, h0 = img.size
        img_resized = img.resize((IMG_SIZE, IMG_SIZE), Image.LANCZOS)
        arr = 1.0 - (np.asarray(img_resized, dtype=np.float32) / 255.0)
        image = torch.from_numpy(arr).unsqueeze(0).unsqueeze(0).to(device)

        gen_rows = model.generate(image, device=device)
        seq_rows = [
            [float(c)] + [float(p) for p in params] + [float(lc), float(wc)]
            for c, params, lc, wc in gen_rows
        ]
        source = SourceInfo(image_width=IMG_SIZE, image_height=IMG_SIZE, kind="scan")
        entities = decode(seq_rows, source, origin="neural")
        # scale predicted (IMG_SIZE-space) entities up to the GT image's own resolution
        scale_x, scale_y = w0 / IMG_SIZE, h0 / IMG_SIZE
        for e in entities:
            _rescale_entity(e, scale_x, scale_y)

        ink = np.asarray(img.convert("L"))
        ink_mask = (255 - ink) > 40
        if not entities:
            recalls.append(0.0)
            precisions.append(0.0)
            continue
        score = score_coverage(entities, ink_mask.astype(np.uint8) * 255, thin_px=2, thick_px=3)
        recalls.append(score.recall)
        precisions.append(score.precision)
    return {"mean_recall": sum(recalls) / len(recalls), "mean_precision": sum(precisions) / len(precisions)}


def _rescale_entity(e, sx: float, sy: float) -> None:
    if hasattr(e, "p1") and e.p1 is not None:
        e.p1.x *= sx
        e.p1.y *= sy
    if hasattr(e, "p2") and e.p2 is not None:
        e.p2.x *= sx
        e.p2.y *= sy
    if hasattr(e, "center") and e.center is not None:
        e.center.x *= sx
        e.center.y *= sy
    if hasattr(e, "radius") and e.radius is not None:
        e.radius *= (sx + sy) / 2
    if hasattr(e, "points"):
        for p in e.points:
            p.x *= sx
            p.y *= sy
    if hasattr(e, "boundary"):
        for p in e.boundary:
            p.x *= sx
            p.y *= sy


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=pathlib.Path, help="build_dataset.py output dir")
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--eval-every", type=int, default=500)
    ap.add_argument("--num-workers", type=int, default=4)
    ap.add_argument("--resume", type=pathlib.Path, default=None)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"device: {device}")

    train_ds = CadSequenceDataset(args.data / "train.jsonl")
    val_ds = CadSequenceDataset(args.data / "val.jsonl")
    print(f"train: {len(train_ds)}  val: {len(val_ds)}")
    train_loader = DataLoader(
        train_ds, batch_size=args.batch_size, shuffle=True, collate_fn=collate,
        num_workers=args.num_workers, drop_last=True, pin_memory=True,
    )
    val_loader = DataLoader(
        val_ds, batch_size=args.batch_size, shuffle=False, collate_fn=collate,
        num_workers=2, pin_memory=True,
    )

    model = CadVectorizerModel().to(device)
    if args.resume and args.resume.exists():
        state = torch.load(args.resume, map_location=device)
        model.load_state_dict(state["model"])
        print(f"resumed from {args.resume} (step {state.get('step', '?')})")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-4)
    total_steps = args.epochs * len(train_loader)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=max(total_steps, 1))

    history = []
    best_holdout_recall = -1.0
    step = 0
    started = time.time()
    for epoch in range(args.epochs):
        model.train()
        for images, dec_in, targets, pad_mask in train_loader:
            images = images.to(device)
            cmd_in, params_in, lc_in, wc_in = (t.to(device) for t in dec_in)
            targets = tuple(t.to(device) for t in targets)
            pad_mask = pad_mask.to(device)

            cmd_logits, param_pred, lc_logits, wc_logits = model(images, cmd_in, params_in, lc_in, wc_in)
            loss, parts = compute_loss(cmd_logits, param_pred, lc_logits, wc_logits, targets, pad_mask)

            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            step += 1

            if step % 50 == 0:
                elapsed = time.time() - started
                print(f"epoch {epoch} step {step}/{total_steps} loss={loss.item():.4f} "
                      f"({parts}) elapsed={elapsed:.0f}s")

            if step % args.eval_every == 0:
                model.eval()
                val_loss = _eval_val_loss(model, val_loader, device)
                holdout = eval_holdout(model, args.data / "holdout.jsonl", args.repo / "backend", device)
                entry = {"step": step, "epoch": epoch, "train_loss": loss.item(), "val_loss": val_loss,
                         "holdout": holdout, "elapsed_s": round(time.time() - started, 1)}
                history.append(entry)
                (args.out / "metrics.json").write_text(json.dumps(history, indent=2))
                ckpt_path = args.out / f"ckpt_step{step}.pt"
                torch.save({"model": model.state_dict(), "step": step}, ckpt_path)
                print(f"  -> checkpoint {ckpt_path.name} | val_loss={val_loss:.4f} | holdout={holdout}")

                if holdout and holdout["mean_recall"] > best_holdout_recall:
                    best_holdout_recall = holdout["mean_recall"]
                    torch.save({"model": model.state_dict(), "step": step}, args.out / "best.pt")
                    print(f"  -> new BEST (holdout recall {best_holdout_recall:.4f})")
                model.train()

    torch.save({"model": model.state_dict(), "step": step}, args.out / "last.pt")
    (args.out / "metrics.json").write_text(json.dumps(history, indent=2))
    print(f"training done: {step} steps, best_holdout_recall={best_holdout_recall:.4f}")
    return 0


@torch.no_grad()
def _eval_val_loss(model, loader, device) -> float:
    total, n = 0.0, 0
    for images, dec_in, targets, pad_mask in loader:
        images = images.to(device)
        cmd_in, params_in, lc_in, wc_in = (t.to(device) for t in dec_in)
        targets = tuple(t.to(device) for t in targets)
        pad_mask = pad_mask.to(device)
        cmd_logits, param_pred, lc_logits, wc_logits = model(images, cmd_in, params_in, lc_in, wc_in)
        loss, _ = compute_loss(cmd_logits, param_pred, lc_logits, wc_logits, targets, pad_mask)
        total += loss.item()
        n += 1
    return total / max(n, 1)


if __name__ == "__main__":
    sys.exit(main())
