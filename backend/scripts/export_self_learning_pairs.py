#!/usr/bin/env python3
"""Export (auto-recognized IR, human-corrected IR) training pairs from
production ``cad_ir_revisions`` history.

Every ``vectorize`` generation's revision 0 is the raw auto-recognition
(``origin="auto"``); later revisions accumulate human review/edit actions
(``origin in ("review", "editor")``). When such a generation ends with at
least one non-auto revision, the gap between revision 0 and the LATEST
revision is exactly the correction signal a future model retrain should
learn from — real production mistakes, not synthetic approximations of them.

This is genuinely higher-value than synthetic data (Ф3.1) precisely because
it comes from cases where the model was WRONG on real input; but it only
exists once the system has real usage history, so this script is
infrastructure for that future retrain, not something with output today on
a fresh install. Output format matches ``tools/cad-dataset/build_dataset.py``
exactly (jsonl manifest + pre-encoded sequence.npy) so it drops straight
into a training run's ``--data`` directory as an extra split, or gets
concatenated into ``train.jsonl``.

Usage:
    python3 export_self_learning_pairs.py --out <dir> [--min-revision 1]

Requires DB access (same env as the backend: POSTGRES_HOST etc).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys


async def _run(out_dir: pathlib.Path, min_revision: int) -> int:
    import numpy as np
    from sqlalchemy import func, select

    from app.ai.cad_ir import CadIR
    from app.ai.cad_ir.sequence import encode
    from app.db.models import CadIrRevision, ImageGeneration
    from app.db.session import _get_session_factory
    from app.storage import download_file

    (out_dir / "sequences" / "self_learning").mkdir(parents=True, exist_ok=True)
    (out_dir / "images" / "self_learning").mkdir(parents=True, exist_ok=True)

    factory = _get_session_factory()
    rows_out = []
    skipped = 0
    async with factory() as db:
        # Generations with at least one revision beyond auto (rev 0).
        max_rev_subq = (
            select(CadIrRevision.generation_id, func.max(CadIrRevision.revision).label("max_rev"))
            .group_by(CadIrRevision.generation_id)
            .having(func.max(CadIrRevision.revision) > 0)
            .subquery()
        )
        gen_ids = (await db.execute(select(max_rev_subq.c.generation_id))).scalars().all()
        print(f"generations with human corrections: {len(gen_ids)}")

        for gen_id in gen_ids:
            revisions = (
                await db.execute(
                    select(CadIrRevision)
                    .where(CadIrRevision.generation_id == gen_id)
                    .order_by(CadIrRevision.revision.asc())
                )
            ).scalars().all()
            if not revisions or revisions[0].revision != 0 or revisions[0].origin != "auto":
                skipped += 1
                continue
            latest = revisions[-1]
            if latest.revision < min_revision or latest.origin not in ("review", "editor"):
                skipped += 1
                continue

            gen = await db.get(ImageGeneration, gen_id)
            if gen is None or not gen.source_image_paths:
                skipped += 1
                continue

            try:
                corrected_ir = CadIR.model_validate_json(download_file(latest.ir_path))
            except Exception as exc:  # noqa: BLE001
                print(f"SKIP {gen_id}: bad corrected IR ({exc})", file=sys.stderr)
                skipped += 1
                continue

            try:
                image_bytes = download_file(gen.source_image_paths[0])
            except Exception as exc:  # noqa: BLE001
                print(f"SKIP {gen_id}: source image unavailable ({exc})", file=sys.stderr)
                skipped += 1
                continue
            image_path = out_dir / "images" / "self_learning" / f"{gen_id}.png"
            image_path.write_bytes(image_bytes)

            seq = np.array(encode(corrected_ir), dtype=np.float32)
            seq_path = out_dir / "sequences" / "self_learning" / f"{gen_id}.npy"
            np.save(seq_path, seq)
            rows_out.append({
                "image": str(image_path.resolve()),
                "sequence": str(seq_path.resolve()),
                "ir": latest.ir_path,
                "generation_id": str(gen_id),
                "auto_revision_ir": revisions[0].ir_path,
                "correction_origin": latest.origin,
                "revisions_span": latest.revision,
            })

    manifest_path = out_dir / "self_learning.jsonl"
    with open(manifest_path, "w") as fh:
        for row in rows_out:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"exported: {len(rows_out)} pairs -> {manifest_path}")
    print(f"skipped: {skipped}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--min-revision", type=int, default=1)
    ap.add_argument("--repo", type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    args = ap.parse_args()
    sys.path.insert(0, str(args.repo))
    args.out.mkdir(parents=True, exist_ok=True)
    return asyncio.run(_run(args.out, args.min_revision))


if __name__ == "__main__":
    sys.exit(main())
