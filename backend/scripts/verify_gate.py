"""Гейт проверяльщиков: храповик на эталонном корпусе по ВСЕМ проверяльщикам.

Прежний гейт мерил один проверяльщик (`dimension_line`) на устаревшем корпусе
v2 одним порогом 0,9 на всех ступенях: на фото и грубых листах порог либо
ложно падал, либо ничего не значил, а регрессия в 5 % на чистом листе
проходила незамеченной.

Здесь база — ТОЧНЫЕ счётчики «найдено / верно» по каждому проверяльщику,
ступени лестницы и случаю (верное чтение, внесённые ошибки). Генерация
корпуса и харнесс детерминированы, поэтому сравнение точное, без допуска на
шум. Гейт падает, если:

* верных стало меньше;
* неверных вердиктов (найдено, но неверно) стало больше — ложное
  опровержение хуже честного «не измеримо»;
* пропал проверяльщик/ступень/случай или изменилось число гипотез.

Улучшение фиксируется только явно: ``--update`` переписывает базу, и её дифф
виден в коммите. Отпечаток корпуса (sha256 файлов dev) хранится в базе —
сравнение с другим корпусом гейт отвергает.

    python3 scripts/verify_gate.py --corpus ../cad-dataset-out/verify-corpus-v9-ladder \\
        --baseline tests/fixtures/verify_gate_v9.json [--update] [--report out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

Counts = dict[str, dict[str, dict[str, list[int]]]]


def fingerprint(corpus: pathlib.Path, rows: list[dict[str, Any]]) -> str:
    """sha256 по именам и содержимому файлов листов (PNG и эталон) в порядке имён."""
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["name"]):
        for suffix in (".png", ".json"):
            path = corpus / f"{row['name']}{suffix}"
            digest.update(path.name.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _evaluate_row(task: tuple[str, str, str]) -> list[tuple[str, str, str, bool, bool]]:
    """Все проверяльщики на одном листе: ``(проверяльщик, ступень, случай, найдено, верно)``."""
    corpus, name, step = task
    import eval_verify

    root = pathlib.Path(corpus)
    png = (root / f"{name}.png").read_bytes()
    truth = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
    png, truth, _dewarped = eval_verify.product_normalize(png, truth)
    results = []
    for verifier, evaluate in sorted(eval_verify._VERIFIERS.items()):
        if verifier == "plate_hole":
            # Как в продукте: план пластины находится по листу, а не из эталона.
            outcomes = eval_verify.eval_plate_hole(png, truth, "sheet")
        else:
            outcomes = evaluate(png, truth)
        for item in outcomes:
            # `dimension_line` проверяет одну верную подпись и случая не пишет.
            case = item.get("case", "truth")
            results.append((verifier, step, case, bool(item["found"]), bool(item.get("correct"))))
    return results


def measure(corpus: pathlib.Path, rows: list[dict[str, Any]], workers: int) -> Counts:
    """Счётчики ``[n, найдено, верно]`` — сумма по листам, от порядка не зависит."""
    counts: Counts = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: [0, 0, 0])))
    tasks = [(str(corpus), row["name"], row["step"]) for row in rows]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for results in pool.map(_evaluate_row, tasks, chunksize=4):
            for verifier, step, case, found, correct in results:
                cell = counts[verifier][step][case]
                cell[0] += 1
                cell[1] += int(found)
                cell[2] += int(found and correct)
    return {v: {s: dict(cases) for s, cases in steps.items()} for v, steps in counts.items()}


def compare(baseline: Counts, current: Counts) -> tuple[list[str], list[str]]:
    """Регрессии и улучшения относительно базы (списки строк для отчёта)."""
    regressions: list[str] = []
    improvements: list[str] = []
    for verifier, steps in sorted(baseline.items()):
        for step, cases in sorted(steps.items()):
            for case, (n, found, correct) in sorted(cases.items()):
                key = f"{verifier}@{step}/{case}"
                got = current.get(verifier, {}).get(step, {}).get(case)
                if got is None:
                    regressions.append(f"{key}: нет в текущем прогоне")
                    continue
                g_n, g_found, g_correct = got
                if g_n != n:
                    regressions.append(f"{key}: гипотез {g_n}, в базе {n}")
                    continue
                wrong, g_wrong = found - correct, g_found - g_correct
                if g_correct < correct:
                    regressions.append(f"{key}: верно {g_correct}/{n}, в базе {correct}")
                if g_wrong > wrong:
                    regressions.append(f"{key}: неверных вердиктов {g_wrong}, в базе {wrong}")
                if g_correct > correct or g_wrong < wrong:
                    improvements.append(
                        f"{key}: верно {correct} → {g_correct}, неверных {wrong} → {g_wrong}"
                    )
    for verifier, steps in sorted(current.items()):
        for step, cases in sorted(steps.items()):
            for case in sorted(cases):
                if case not in baseline.get(verifier, {}).get(step, {}):
                    improvements.append(f"{verifier}@{step}/{case}: новое, в базе нет")
    return regressions, improvements


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--baseline", type=pathlib.Path, required=True)
    parser.add_argument("--update", action="store_true", help="переписать базу текущим прогоном")
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in (args.corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [row for row in rows if row.get("split") == "dev"]
    corpus_print = fingerprint(args.corpus, rows)
    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.exists() else None
    )
    if baseline and not args.update and baseline.get("fingerprint") != corpus_print:
        print(
            f"корпус не тот, с которым снята база: {corpus_print[:12]} против "
            f"{str(baseline.get('fingerprint'))[:12]} — пересоберите корпус или обновите базу"
        )
        return 2
    current = measure(args.corpus, rows, args.workers)
    if args.update or baseline is None:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(
            json.dumps(
                {"corpus": args.corpus.name, "fingerprint": corpus_print, "counts": current},
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"база записана: {args.baseline}")
        return 0
    regressions, improvements = compare(baseline["counts"], current)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(
                {"regressions": regressions, "improvements": improvements, "counts": current},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
    for line in improvements:
        print(f"лучше: {line}")
    for line in regressions:
        print(f"ХУЖЕ: {line}")
    if regressions:
        print(f"гейт не пройден: {len(regressions)} регрессий")
        return 1
    print("гейт пройден" + (" — есть улучшения, зафиксируйте --update" if improvements else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
