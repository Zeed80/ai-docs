#!/usr/bin/env python3
"""Pack (image, ground-truth CadIR) pairs from synth_ir.py / dwg_to_ir.py
into train/val/holdout manifests + pre-encoded sequence targets for the
neural vectorizer trainer.

Split policy (per the plan — "holdout — только реальные чертежи, не
синтетика"): ALL synthetic clean targets are partitioned train/val by stem
(so degraded variants of one target never leak across the split); ALL
holdout entries come from real DWG-derived ground truth and are NEVER used
for training, gradient updates, or model selection during training — only
for the final "did it beat the CV baseline" comparison (Ф3.5).

Usage:
    python3 build_dataset.py --synth <synth_ir.py --out dir> \
        --holdout <dwg_to_ir.py --out dir> --out <packed dataset dir> \
        [--val-fraction 0.1] [--seed 0]

Output:
    <out>/train.jsonl, val.jsonl, holdout.jsonl  — one row per sample:
        {"image": "<abs path to control/clean PNG>",
         "sequence": "<abs path to .npy target>",
         "ir": "<abs path to source ir.json>"}
    <out>/sequences/<split>/<stem>.npy
    <out>/vocab.json  — COMMANDS / N_PARAMS snapshot (for the trainer to pin against)
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--synth", required=True, type=pathlib.Path)
    ap.add_argument("--holdout", required=True, type=pathlib.Path)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--val-fraction", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--image-source",
        choices=("control", "clean", "both"),
        default="control",
        help="training raster source; web STEP corpus uses degraded clean tiles",
    )
    ap.add_argument(
        "--split-manifest",
        type=pathlib.Path,
        help="optional source-grouped manifest.jsonl; synthetic holdout rows are excluded",
    )
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[2])
    args = ap.parse_args()

    sys.path.insert(0, str(args.repo / "backend"))
    import numpy as np

    from app.ai.cad_ir.schema import CadIR
    from app.ai.cad_ir.sequence import COMMANDS, N_PARAMS, encode
    from app.ai.cad_profile import choose_profile

    args.out.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val", "holdout"):
        (args.out / "sequences" / split).mkdir(parents=True, exist_ok=True)

    (args.out / "vocab.json").write_text(
        json.dumps({"commands": list(COMMANDS), "n_params": N_PARAMS}, indent=2)
    )

    rng = random.Random(args.seed)
    split_rows: dict[str, dict] = {}
    if args.split_manifest:
        with args.split_manifest.open() as stream:
            for line in stream:
                row = json.loads(line)
                split_rows[row["id"]] = row

    # ── Synthetic: split by clean stem, one sequence per stem, many degraded
    # image variants sharing it ────────────────────────────────────────────
    ir_dir = args.synth / "ir"
    control_dir = args.synth / "control"
    stems = sorted(p.stem for p in ir_dir.glob("*.json"))
    rng.shuffle(stems)
    n_val = max(1, round(len(stems) * args.val_fraction)) if stems else 0
    val_stems = set(stems[:n_val])

    rows = {"train": [], "val": [], "holdout": []}
    skipped = 0
    for stem in stems:
        split_meta = split_rows.get(stem)
        if split_rows and split_meta is None:
            print(f"SKIP {stem}: absent from split manifest", file=sys.stderr)
            skipped += 1
            continue
        if split_meta and split_meta["split"] == "holdout":
            # The final holdout remains real-only. Synthetic holdout groups
            # are reserved for corpus audits and must not enter optimization.
            continue
        ir_path = ir_dir / f"{stem}.json"
        try:
            ir = CadIR.model_validate_json(ir_path.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP {stem}: invalid IR ({exc})", file=sys.stderr)
            skipped += 1
            continue
        split = split_meta["split"] if split_meta else ("val" if stem in val_stems else "train")
        seq = np.array(encode(ir), dtype=np.float32)
        seq_path = args.out / "sequences" / split / f"{stem}.npy"
        np.save(seq_path, seq)

        control_variants = sorted(control_dir.glob(f"{stem}__v*.png"))
        clean_path = args.synth / "clean" / f"{stem}.png"
        variants = []
        if args.image_source in ("clean", "both") and clean_path.exists():
            variants.append(clean_path)
        if args.image_source in ("control", "both"):
            variants.extend(control_variants)
        if not variants:
            print(f"SKIP {stem}: no degraded variants found", file=sys.stderr)
            skipped += 1
            continue
        for variant in variants:
            row = {
                "image": str(variant.resolve()),
                "sequence": str(seq_path.resolve()),
                "ir": str(ir_path.resolve()),
            }
            if split_meta:
                row.update(
                    {
                        "profile": split_meta["profile"],
                        "source_group_id": split_meta["source_group_id"],
                        "truth_kind": split_meta["kind"],
                    }
                )
            rows[split].append(row)

    # ── Holdout: real DWG-derived, clean render as input, never trained on ──
    h_ir_dir = args.holdout / "ir"
    h_clean_dir = args.holdout / "clean"
    for ir_path in sorted(h_ir_dir.glob("*.json")):
        stem = ir_path.stem
        clean_path = h_clean_dir / f"{stem}.png"
        if not clean_path.exists():
            print(f"SKIP holdout {stem}: no clean render", file=sys.stderr)
            skipped += 1
            continue
        try:
            ir = CadIR.model_validate_json(ir_path.read_text())
        except Exception as exc:  # noqa: BLE001
            print(f"SKIP holdout {stem}: invalid IR ({exc})", file=sys.stderr)
            skipped += 1
            continue
        seq = np.array(encode(ir), dtype=np.float32)
        seq_path = args.out / "sequences" / "holdout" / f"{stem}.npy"
        np.save(seq_path, seq)
        rows["holdout"].append({
            "image": str(clean_path.resolve()),
            "sequence": str(seq_path.resolve()),
            "ir": str(ir_path.resolve()),
            "profile": choose_profile("auto", [], stem).profile,
            "source_group_id": f"real_holdout:{stem}",
            "truth_kind": "real_local_holdout",
        })

    for split, items in rows.items():
        with open(args.out / f"{split}.jsonl", "w") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"{split}: {len(items)} samples")
    print(f"skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
