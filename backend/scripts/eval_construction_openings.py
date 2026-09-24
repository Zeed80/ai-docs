#!/usr/bin/env python3
"""Ф7.3: проёмы плана (разрыв стены, дверь по дуге, окно по остеклению)
против эталона синтетических планов (`verify_corpus.construction_plan`).

Масштаб — эталонный: цепочку между осями проверяет `eval_construction_walls`
(живой масштаб там совпал с эталоном до 0,05 %). Здесь мерится только то,
что добавляет Ф7.3.

    python scripts/eval_construction_openings.py --seeds 40 --out /tmp/openings.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_POSITION_SHARE = 0.75
_EDGE_SHARE = 0.15


def score(truth: list[dict], found: list[dict]) -> dict:
    pool = list(found)
    result = {"truth": len(truth), "found": len(found), "hits": 0, "kind_right": 0}
    confusion: dict[str, int] = {}
    for item in truth:
        width = item["end"] - item["start"]
        match = None
        for candidate in pool:
            gap = candidate["gap"]
            if gap.axis != item["axis"]:
                continue
            if abs(gap.position - item["position"]) > max(_POSITION_SHARE * item["thickness"], 2):
                continue
            tolerance = max(_EDGE_SHARE * width, 3.0)
            if abs(gap.start - item["start"]) > tolerance or abs(gap.end - item["end"]) > tolerance:
                continue
            match = candidate
            break
        if match is None:
            key = f"{item['kind']}→пропущен"
            confusion[key] = confusion.get(key, 0) + 1
            continue
        pool.remove(match)
        result["hits"] += 1
        result["kind_right"] += match["kind"] == item["kind"]
        key = f"{item['kind']}→{match['kind']}"
        confusion[key] = confusion.get(key, 0) + 1
    result["extra"] = len(pool)
    for candidate in pool:
        key = f"лишний→{candidate['kind']}"
        confusion[key] = confusion.get(key, 0) + 1
    result["confusion"] = confusion
    return result


def main() -> int:
    import numpy as np

    from app.ai.construction_walls import find_openings, find_walls
    from app.ai.verify_corpus.construction_plan import random_plan

    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=40)
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--out", type=pathlib.Path)
    args = parser.parse_args()
    rows = []
    for seed in range(args.first, args.first + args.seeds):
        image, truth = random_plan(seed)
        gray = np.asarray(image)
        walls = find_walls(gray, truth.mm_per_px)
        found = find_openings(gray, walls, truth.mm_per_px)
        from app.ai.construction_axes import axis_markers
        from scripts.eval_construction_walls import score_markers

        row = {
            "seed": seed,
            **score(truth.openings, found),
            "markers": score_markers(truth.markers, axis_markers(gray)),
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    total: dict = {"truth": 0, "found": 0, "hits": 0, "kind_right": 0, "extra": 0}
    confusion: dict[str, int] = {}
    for row in rows:
        for key in total:
            total[key] += row[key]
        for key, value in row["confusion"].items():
            confusion[key] = confusion.get(key, 0) + value
    total["confusion"] = dict(sorted(confusion.items()))
    total["markers"] = {
        key: sum(row["markers"][key] for row in rows) for key in ("truth", "found", "hits", "extra")
    }
    print(json.dumps({"ИТОГ": total}, ensure_ascii=False))
    if args.out:
        args.out.write_text(
            json.dumps({"rows": rows, "total": total}, ensure_ascii=False, indent=1)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
