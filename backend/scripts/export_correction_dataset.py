#!/usr/bin/env python3
"""Export user corrections to JSONL dataset for Qwen3 fine-tuning.

Usage:
    python scripts/export_correction_dataset.py --output corrections_dataset.jsonl
    python scripts/export_correction_dataset.py --output corrections_dataset.jsonl --mark-used
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path


async def _export(output_path: Path, mark_used: bool, min_records: int) -> int:
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from app.db.session import _get_session_factory
    from app.db.models import DrawingFeatureCorrection
    from sqlalchemy import select

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(DrawingFeatureCorrection)
            .where(DrawingFeatureCorrection.corrected_type.isnot(None))
            .order_by(DrawingFeatureCorrection.created_at.asc())
        )
        corrections = result.scalars().all()

    if not corrections:
        print("Нет коррекций для экспорта.")
        return 0

    records = []
    for c in corrections:
        surrounding = (c.context_json or {}).get("surrounding_types", [])
        context_str = (
            f"Окружающие элементы: {', '.join(surrounding[:5])}" if surrounding else ""
        )

        user_content = (
            f"Элемент чертежа: {c.original_name}\n"
            f"Тип чертежа: {c.drawing_type}\n"
            f"VLM определил как: {c.original_type} (уверенность {c.confidence_at_correction:.0%})\n"
        )
        if c.source_view:
            user_content += f"Вид: {c.source_view}\n"
        if context_str:
            user_content += f"{context_str}\n"
        user_content += "\nКаким на самом деле является этот элемент?"

        assistant_content = json.dumps(
            {
                "feature_type": c.corrected_type,
                **({"name": c.corrected_name} if c.corrected_name else {}),
                **({"note": c.note} if c.note else {}),
            },
            ensure_ascii=False,
        )

        records.append(
            {
                "messages": [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": assistant_content},
                ],
                "metadata": {
                    "drawing_type": c.drawing_type,
                    "original_type": c.original_type,
                    "corrected_type": c.corrected_type,
                    "confidence_at_correction": c.confidence_at_correction,
                    "correction_id": str(c.id),
                    "exported_at": datetime.utcnow().isoformat(),
                },
            }
        )

    if len(records) < min_records:
        print(
            f"Только {len(records)} записей — меньше порога {min_records}. "
            "Накопите больше коррекций перед fine-tuning."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Экспортировано {len(records)} записей → {output_path}")

    if mark_used and corrections:
        async with _get_session_factory()() as db:
            from sqlalchemy import update
            ids = [c.id for c in corrections]
            await db.execute(
                update(DrawingFeatureCorrection)
                .where(DrawingFeatureCorrection.id.in_(ids))
                .values(used_as_few_shot=True)
            )
            await db.commit()
        print(f"Отмечено {len(ids)} записей как used_as_few_shot=True")

    return len(records)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export drawing feature corrections to JSONL for Qwen3 fine-tuning"
    )
    parser.add_argument(
        "--output",
        default="corrections_dataset.jsonl",
        help="Output JSONL file path (default: corrections_dataset.jsonl)",
    )
    parser.add_argument(
        "--mark-used",
        action="store_true",
        help="Mark exported corrections as used_as_few_shot=True",
    )
    parser.add_argument(
        "--min-records",
        type=int,
        default=50,
        help="Warn if fewer than N records (default: 50)",
    )
    args = parser.parse_args()

    count = asyncio.run(
        _export(
            output_path=Path(args.output),
            mark_used=args.mark_used,
            min_records=args.min_records,
        )
    )
    sys.exit(0 if count > 0 else 1)


if __name__ == "__main__":
    main()
