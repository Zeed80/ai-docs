"""Калибровка предохранителя апскейла (`sheet_upscale.agreement`) на корпусе.

Пары «грубый лист / он же после SeedVR2» из опытного корпуса — распределение
совпадения; отрицательная проверка — на увеличенном листе одна подпись
размера заменена чужой (как если бы модель перерисовала цифру): худшая плитка
обязана упасть ниже порога.

    python3 scripts/calibrate_sheet_upscale.py --corpus ../cad-dataset-out/verify-corpus-v9-sr
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def main() -> int:
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.sheet_upscale import MIN_TILE_AGREEMENT, agreement

    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--shift", type=int, default=0, help="допуск сдвига плитки, px")
    args = parser.parse_args()
    rows = [
        json.loads(line)
        for line in (args.corpus / "manifest.jsonl").read_text().splitlines()
        if line.strip()
    ]
    names = {row["name"] for row in rows}
    good, forged = [], []
    for row in rows:
        if not row["step"].startswith("sr-"):
            continue
        low_name = f"{row['sheet']}@dpi-{row['step'][3:]}"
        if low_name not in names:
            continue
        low = np.asarray(Image.open(args.corpus / f"{low_name}.png").convert("L"))
        sr = np.asarray(Image.open(args.corpus / f"{row['name']}.png").convert("L")).copy()
        good.append((row["name"], agreement(low, sr, shift_px=args.shift)))
        # Подделка: подпись одного размера заменить подписью другого.
        truth = json.loads((args.corpus / f"{row['name']}.json").read_text())
        boxes = [
            [int(v) for v in label["label"]["bbox_px"]]
            for label in truth["labels"]
            if label.get("kind") == "dimension" and (label.get("label") or {}).get("bbox_px")
        ]
        pairs = [
            (a, b)
            for a in boxes
            for b in boxes
            if a != b
            and abs((a[2] - a[0]) - (b[2] - b[0])) <= 4
            and abs((a[3] - a[1]) - (b[3] - b[1])) <= 4
        ]
        if not pairs:
            continue
        (ax0, ay0, ax1, ay1), (bx0, by0, _bx1, _by1) = pairs[0]
        patch = sr[by0 : by0 + (ay1 - ay0), bx0 : bx0 + (ax1 - ax0)].copy()
        sr[ay0 : ay0 + patch.shape[0], ax0 : ax0 + patch.shape[1]] = patch
        forged.append((row["name"], agreement(low, sr, shift_px=args.shift)))

    def show(title: str, items: list) -> None:
        worst = sorted(item[1]["worst_tile"] for item in items)
        p1 = sorted(item[1]["tile_p1"] for item in items)
        print(
            f"{title}: n={len(items)}  худшая плитка: мин {worst[0]:.3f} "
            f"p10 {worst[len(worst) // 10]:.3f} медиана {worst[len(worst) // 2]:.3f} "
            f"макс {worst[-1]:.3f} | 1-й процентиль плиток: мин {p1[0]:.3f}"
        )

    show("настоящие пары", good)
    show("с подменённой подписью", forged)
    passed_forged = [name for name, score in forged if score["worst_tile"] >= MIN_TILE_AGREEMENT]
    rejected_good = [name for name, score in good if score["worst_tile"] < MIN_TILE_AGREEMENT]
    print(
        f"порог {MIN_TILE_AGREEMENT}: отвергнуто настоящих {len(rejected_good)} "
        f"{rejected_good[:6]}; пропущено подделок {len(passed_forged)} {passed_forged[:6]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
