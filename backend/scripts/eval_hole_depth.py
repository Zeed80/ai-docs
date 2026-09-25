#!/usr/bin/env python3
"""Глубина отверстий пластины по виду на толщину против эталона корпуса.

Случаи на каждое отверстие: верное чтение; перевёрнутое (сквозное прочитано
глухим на половину толщины, глухое — сквозным); глубина +3 мм у глухого.
Верно — подтверждено верное и опровергнуто искажённое.

    python scripts/eval_hole_depth.py --corpus <каталог лестницы или листов>
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def cases(profile: dict) -> list[tuple[str, int, dict]]:
    out = []
    thickness = float(profile["thickness_mm"])
    for index, hole in enumerate(profile.get("holes") or []):
        out.append(("truth", index, profile))
        flipped = copy.deepcopy(profile)
        target = flipped["holes"][index]
        if target.get("depth_mm"):
            target.pop("depth_mm")
        else:
            target["depth_mm"] = round(thickness / 2.0, 1)
        out.append(("flip", index, flipped))
        if hole.get("depth_mm"):
            deeper = copy.deepcopy(profile)
            deeper["holes"][index]["depth_mm"] = float(hole["depth_mm"]) + 3.0
            out.append(("depth", index, deeper))
    return out


def main() -> int:
    import eval_verify
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers.hole_depth import verify_hole_depths

    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--glob", default="plate-*.json")
    args = parser.parse_args()
    table: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for path in sorted(args.corpus.glob(args.glob)):
        step = path.stem.split("@")[1] if "@" in path.stem else "clean"
        truth = json.loads(path.read_text())
        png, truth, _ = eval_verify.product_normalize(path.with_suffix(".png").read_bytes(), truth)
        gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
        profile = truth["spec"]["main_view"]["profile"]
        if not profile.get("holes") or not profile.get("thickness_mm"):
            continue
        memo: dict[int, list] = {}
        for case, index, read in cases(profile):
            key = id(read)
            if key not in memo:
                memo[key] = verify_hole_depths(gray, read)
            verdict = memo[key][index]
            cell = table[(step, case)]
            cell[0] += 1
            if verdict["status"] != "unmeasurable":
                cell[1] += 1
                want = "confirmed" if case == "truth" else "refuted"
                cell[2] += verdict["status"] == want
    for (step, case), (n, found, correct) in sorted(table.items()):
        print(
            f"{step:10s} {case:6s} n={n:3d} измерено {found:3d} верно {correct:3d} неверно {found - correct}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
