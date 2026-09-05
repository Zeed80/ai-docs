"""Export (sheet, read, human correction) triples for reader fine-tuning.

This is the corpus item 6 of the digitize plan needs. It is deliberately NOT
the same thing as ``export_self_learning_pairs.py``: that one exports GEOMETRY
corrections for an image-to-geometry model, the target that already failed
this project at entity F1 0.000. What the literature shows beating frontier
models on drawings is field EXTRACTION — so this exports what the reader said
about each field and what a person corrected it to.

Reports coverage as well as volume: a count of examples says nothing about
whether they cover the fields that actually fail.

    python backend/scripts/export_reader_corrections.py --out data/reader-corpus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


async def _run(out_dir: pathlib.Path) -> int:
    from sqlalchemy import select

    from app.ai.cad_reader_feedback import corpus_summary
    from app.db.models import ImageGeneration
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    records: list[dict] = []
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(ImageGeneration).where(ImageGeneration.operation == "vectorize")
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            record = (row.params or {}).get("spec_correction_record")
            if record:
                records.append(record)

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "reader_corrections.jsonl"
    with manifest.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary = corpus_summary(records)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not records:
        print(
            "\nКорпус пуст: правок чтения ещё не вносили. Это ожидаемо до того, "
            "как система поработает с людьми — обучать пока не на чем.",
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/reader-corpus")
    args = parser.parse_args()
    return asyncio.run(_run(pathlib.Path(args.out)))


if __name__ == "__main__":
    raise SystemExit(main())
