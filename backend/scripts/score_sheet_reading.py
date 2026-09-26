"""Оценка этапа A против эталона `truth_reading.json`: тип листа и главный вид.

Главный вид верен, если центр эталона лежит внутри рамки, названной главной."""

import json
import pathlib
import sys

corpus = pathlib.Path(sys.argv[1])
truth = json.loads((corpus / "truth_reading.json").read_text())
kind_ok = main_ok = main_n = 0
for sid, t in truth.items():
    r = json.loads((corpus / "reading" / f"{sid}.json").read_text())
    kind_ok += r["sheet_kind"] == t["sheet_kind"]
    if t["main_center"]:
        main_n += 1
        box = next((x["box"] for x in r["regions"] if x["n"] == r.get("main")), None)
        cx, cy = t["main_center"]
        hit = bool(box) and box[0] <= cx <= box[2] and box[1] <= cy <= box[3]
        # рамка во весь лист — не попадание
        hit = hit and (box[2] - box[0]) * (box[3] - box[1]) < 0.5 * 1e12
        main_ok += hit
        if not hit:
            print("главный вид мимо:", sid)
    if r["sheet_kind"] != t["sheet_kind"]:
        print("тип листа:", sid, r["sheet_kind"], "≠", t["sheet_kind"])
print(f"тип листа {kind_ok}/{len(truth)}, главный вид {main_ok}/{main_n}")
