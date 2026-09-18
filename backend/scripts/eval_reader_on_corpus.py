#!/usr/bin/env python3
"""Базовая линия чтения на корпусе с эталоном (план, задача M5; живой режим M1).

Прогоняет ридер продукта (`read_spec_best_effort` — тот же вызов, что в
`cad_trace` для метода «по описанию», после той же нормализации исходника) по
листам корпуса и сравнивает спек с эталоном генератора поле за полем
(`verify_corpus.score`). Модель — та, что назначена на слот чтения чертежа;
облако не используется.

Медленно: минуты на лист. Результат дописывается построчно в
``<out>.jsonl``, повторный запуск пропускает уже прочитанные листы — прогон
можно прерывать.

Запуск внутри контейнера backend:

    python scripts/eval_reader_on_corpus.py --corpus /tmp/verify-corpus-v2 \\
        --per-kind 2 --out /tmp/reader-baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


async def _run(args: argparse.Namespace) -> int:
    from app.ai.cad_recognize.spec_fragments import read_spec_best_effort
    from app.ai.verify_corpus.score import score_spec, summarize
    from scripts.eval_verify import product_normalize

    rows = [
        json.loads(line)
        for line in (args.corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [
        row
        for row in rows
        if row.get("status", "built") == "built"
        and (args.split == "all" or row.get("split") == args.split)
        and (args.step is None or row.get("step") == args.step)
    ]
    picked: list[dict] = []
    per_kind: dict[str, int] = {}
    for row in sorted(rows, key=lambda item: (item.get("kind") or "", item.get("seed") or 0)):
        kind = row.get("kind") or "?"
        if args.kinds and kind not in args.kinds:
            continue
        if per_kind.get(kind, 0) >= args.per_kind:
            continue
        per_kind[kind] = per_kind.get(kind, 0) + 1
        picked.append(row)

    log = args.out.with_suffix(".jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    done = {
        json.loads(line)["sheet"]
        for line in (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])
        if line.strip()
    }
    for row in picked:
        name = row["name"]
        if name in done:
            continue
        png = (args.corpus / f"{name}.png").read_bytes()
        truth = json.loads((args.corpus / f"{name}.json").read_text(encoding="utf-8"))
        png, truth, dewarped = product_normalize(png, truth)
        started = time.monotonic()
        error = None
        try:
            spec = await read_spec_best_effort(
                png,
                passes=args.passes,
                budget_seconds=args.budget,
                digitization_type=args.digitization_type,
            )
        except Exception as exc:  # noqa: BLE001 — один лист, не весь прогон
            spec, error = {}, f"{type(exc).__name__}: {exc}"[:300]
        seconds = round(time.monotonic() - started, 1)
        score = score_spec(truth["spec"], spec)
        record = {
            "sheet": name,
            "sheet_kind": row.get("kind"),
            "seconds": seconds,
            "dewarped": dewarped,
            "error": error,
            "score": score,
            "unresolved": (spec or {}).get("unresolved") or [],
            "read_main_view": (spec or {}).get("main_view"),
            # Сварной узел (X3) читается телами и швами — без них ошибку в
            # узле не разобрать (weldment-1: одна пластина мимо, какая — неясно).
            "read_parts": (spec or {}).get("parts") or [],
            "read_welds": (spec or {}).get("welds") or [],
            # Что модель выписала с листа: без этого не отличить «не прочитала
            # R5» от «прочитала, но не назначила роль» (базовая линия v4).
            "read_dimensions": [
                str((item or {}).get("value") or "")
                for item in (spec or {}).get("dimensions") or []
            ],
        }
        with log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        print(f"{name:14s} {seconds:7.1f}s  {json.dumps(score, ensure_ascii=False)}", flush=True)

    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    report = {
        "contract": "reader-on-corpus-v1",
        "passes": args.passes,
        "digitization_type": args.digitization_type,
        "sheets": len(records),
        "seconds_total": round(sum(item["seconds"] for item in records), 1),
        "summary": summarize(records),
    }
    args.out.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=1))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True, help="без расширения")
    parser.add_argument("--per-kind", type=int, default=2)
    parser.add_argument("--kinds", nargs="*")
    parser.add_argument("--split", default="dev", choices=("dev", "holdout", "all"))
    parser.add_argument("--step", help="ступень лестницы (для корпуса-лестницы)")
    parser.add_argument("--passes", type=int, default=5, help="как у продукта по умолчанию")
    parser.add_argument("--budget", type=float, default=750.0)
    parser.add_argument("--digitization-type", default="auto")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
