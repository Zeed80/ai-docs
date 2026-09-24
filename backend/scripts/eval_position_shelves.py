#!/usr/bin/env python3
"""Полки выносок с номерами позиций (Ф2 `leader_label`) против эталона корпуса.

Эталон — надписи листа, как нарисованы (`labels` корпуса): номер позиции —
текст-число не больше числа тел, не принадлежащий размеру. Найдено — полка,
вырез которой накрывает рамку номера; лишнее — полка без номера под ней.

    python scripts/eval_position_shelves.py --corpus /tmp/wv
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def truth_positions(label_data: dict) -> list[list[float]]:
    parts = len((label_data.get("spec") or {}).get("parts") or [])
    boxes = []
    for label in label_data.get("labels") or []:
        if label.get("kind") != "text":
            continue
        text = str(label.get("text") or "").strip()
        if text.isdigit() and 1 <= int(text) <= parts:
            boxes.append(label["bbox_px"])
    return boxes


def score(truth: list[list[float]], shelves: list[dict], size: tuple[int, int]) -> dict:
    from app.ai.cad_recognize.verifiers.leader_label import shelf_crop_box

    crops = [shelf_crop_box(item, size) for item in shelves]
    pool = list(range(len(crops)))
    hits = 0
    for box in truth:
        cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
        match = next(
            (
                i
                for i in pool
                if crops[i][0] <= cx <= crops[i][2] and crops[i][1] <= cy <= crops[i][3]
            ),
            None,
        )
        if match is not None:
            pool.remove(match)
            hits += 1
    return {"truth": len(truth), "found": len(shelves), "hits": hits, "extra": len(pool)}


def main() -> int:
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers.leader_label import position_shelves

    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--glob", default="*.json")
    args = parser.parse_args()
    total = {"truth": 0, "found": 0, "hits": 0, "extra": 0}
    for path in sorted(args.corpus.glob(args.glob)):
        if path.name == "manifest.jsonl":
            continue
        data = json.loads(path.read_text())
        if "labels" not in data:
            continue
        image = Image.open(path.with_suffix(".png")).convert("L")
        row = score(truth_positions(data), position_shelves(np.asarray(image)), image.size)
        print(json.dumps({"sheet": path.stem, **row}, ensure_ascii=False), flush=True)
        for key in total:
            total[key] += row[key]
    print(json.dumps({"ИТОГ": total}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
