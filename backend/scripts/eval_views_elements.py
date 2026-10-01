"""Сверка метода `views` с эталоном реальных листов ПО ЭЛЕМЕНТАМ.

Габарит (L, наибольший Ø) совпадает и у тела с буртиком не на месте
(part_02: буртик на 25,9…40,5 вместо 17,5…33,5 при верных L58 Ø6,5) —
поэтому сверяются ступени (Ø и обе границы) и элементы с положением.

Эталон — tests/fixtures/views_element_truth.json (выписан вручную по
надписям листов). Чтение листа моделью (рамки, надписи) берётся из
сохранённых отчётов — без повторного вызова модели:

    python scripts/eval_views_elements.py --readings /tmp/views-out14 --images /tmp/vp

Ступень верна, если на теле есть площадка того же Ø (±1 %, не меньше
0,15 мм) с обеими границами в допуске (±1 мм или 1 % длины). Деталь
сверяется в обеих ориентациях (лист бывает повёрнут) — берётся лучшая.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "tests/fixtures/views_element_truth.json"


def truth_segments(steps: list[list[Any]]) -> list[tuple[float, float, float | None]]:
    """[(начало, конец, Ø)] — соседние ступени одного Ø (разные допуски) слиты."""
    out: list[tuple[float, float, float | None]] = []
    z = 0.0
    for d, length in steps:
        if out and d is not None and out[-1][2] == d:
            out[-1] = (out[-1][0], z + length, d)
        else:
            out.append((z, z + length, d))
        z += length
    return out


def our_plateaus(outer: list[dict[str, float]]) -> list[tuple[float, float, float]]:
    out = []
    for a, b in zip(outer, outer[1:]):
        if abs(float(a["r"]) - float(b["r"])) <= 1e-6 and float(b["z"]) - float(a["z"]) > 0.3:
            d = 2.0 * float(a["r"])
            if out and abs(out[-1][2] - d) <= 1e-6 and abs(out[-1][1] - float(a["z"])) <= 1e-6:
                out[-1] = (out[-1][0], float(b["z"]), d)
            else:
                out.append((float(a["z"]), float(b["z"]), d))
    return out


def flip(
    items: list[tuple[float, float, float]], length: float
) -> list[tuple[float, float, float]]:
    return sorted((length - b, length - a, d) for a, b, d in items)


def radius_at(outer: list[dict[str, float]], z: float) -> float | None:
    """Ø тела на станции z (по площадкам и конусам профиля)."""
    for a, b in zip(outer, outer[1:]):
        za, zb = float(a["z"]), float(b["z"])
        if za <= z <= zb and zb > za:
            t = (z - za) / (zb - za)
            return 2.0 * (float(a["r"]) + t * (float(b["r"]) - float(a["r"])))
    return None


def score_steps(
    truth: list[tuple], outer: list[dict[str, float]], length: float, skip: list[tuple]
) -> tuple[int, int]:
    """Ступень верна, если на 90 % её внутренней части (без 1 мм у границ и
    без канавок эталона) Ø тела совпадает с эталонным. Канавка внутри
    ступени площадку разрезает — это не ошибка ступени."""
    margin = max(1.0, 0.01 * length)
    known = [t for t in truth if t[2] is not None]
    hit = 0
    for t0, t1, td in known:
        dtol = max(0.15, 0.01 * td)
        samples = []
        z = t0 + margin
        while z <= t1 - margin + 1e-9:
            if not any(lo - 0.5 <= z <= hi + 0.5 for lo, hi in skip):
                samples.append(z)
            z += 0.25
        if not samples:
            samples = [(t0 + t1) / 2.0]
        good = sum(
            1 for z in samples if (d := radius_at(outer, z)) is not None and abs(d - td) <= dtol
        )
        if good >= 0.9 * len(samples):
            hit += 1
    return hit, len(known)


def our_keyways(features: list[dict[str, Any]]) -> list[tuple[float, float, float]]:
    out = []
    for f in features:
        if f.get("keyway"):
            z0, z1 = f["keyway"]
            out.append((float(z0), float(z1) - float(z0), float(f.get("height_mm") or 0.0)))
    return out


def our_cross_holes(features: list[dict[str, Any]]) -> list[tuple[float, float]]:
    out = []
    for f in features:
        axis = f.get("axis") or [0, 0, 1]
        if f.get("kind") == "hole" and abs(float(axis[2])) < 0.5 and f.get("diameter_mm"):
            out.append((float(f["origin_mm"][2]), float(f["diameter_mm"])))
    return out


def evaluate(part: dict[str, Any], result: Any) -> dict[str, Any]:
    length = float(part["length_mm"])
    truth = truth_segments(part["steps"])
    keyways = [f for f in part["features"] if f["kind"] == "keyway"]
    holes = [f for f in part["features"] if f["kind"] == "cross_hole" and f.get("at") is not None]
    report: dict[str, Any] = {
        "steps": [0, len([t for t in truth if t[2] is not None])],
        "keyways": [0, len(keyways)],
        "cross_holes": [0, len(holes)],
    }
    if not result.ok:
        report["refused"] = result.reason[:160]
        return report
    outer = result.profile.get("outer") or []
    ours_len = max((float(p["z"]) for p in outer), default=0.0)
    report["length"] = round(ours_len, 2)
    report["max_d"] = round(2 * max((float(p["r"]) for p in outer), default=0.0), 2)
    kws = our_keyways(result.features)
    chs = our_cross_holes(result.features)
    skip = [
        (float(f["at"]), float(f["at"]) + float(f["width"]))
        for f in part["features"]
        if f["kind"] == "groove"
    ]
    best = None
    for mirrored in (False, True):
        profile = (
            sorted(
                ({"z": ours_len - float(q["z"]), "r": float(q["r"])} for q in outer),
                key=lambda q: q["z"],
            )
            if mirrored
            else outer
        )
        k = [(ours_len - (z + n), n, w) for z, n, w in kws] if mirrored else kws
        h = [(ours_len - z, d) for z, d in chs] if mirrored else chs
        steps = score_steps(truth, profile, length, skip)
        kw_hit = sum(
            1
            for f in keyways
            if any(
                abs(z - f["start"]) <= 1.5
                and abs(n - f["length"]) <= 1.5
                and abs(w - f["width"]) <= 0.5
                for z, n, w in k
            )
        )
        ch_hit = sum(
            1
            for f in holes
            if any(abs(z - f["at"]) <= 1.0 and abs(d - f["d"]) <= 0.3 for z, d in h)
        )
        key = (steps[0] + kw_hit + ch_hit, not mirrored)
        if best is None or key > best[0]:
            best = (key, mirrored, steps, kw_hit, ch_hit)
    _key, mirrored, steps, kw_hit, ch_hit = best
    report["steps"] = list(steps)
    report["keyways"][0] = kw_hit
    report["cross_holes"][0] = ch_hit
    report["mirrored"] = mirrored
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readings", required=True, help="каталог сохранённых чтений <id>.json")
    parser.add_argument("--images", required=True, help="каталог листов <id>.png")
    parser.add_argument("--truth", default=str(TRUTH))
    parser.add_argument("--split", default="dev", choices=("dev", "holdout", "all"))
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    import cv2

    sys.path.insert(0, str(ROOT))
    from app.ai.cad_views.pipeline import choose_body
    from app.ai.cad_views.sheet_reading import Region, SheetReading

    parts = json.load(open(args.truth, encoding="utf-8"))["parts"]
    totals = {"steps": [0, 0], "keyways": [0, 0], "cross_holes": [0, 0]}
    rows = {}
    for name, part in sorted(parts.items()):
        if part.get("class") != "rotation":
            continue
        if args.split != "all" and part.get("split") != args.split:
            continue
        reading = Path(args.readings) / f"{name}.json"
        image = Path(args.images) / f"{name}.png"
        if not reading.exists() or not image.exists():
            rows[name] = {"skipped": "нет сохранённого чтения или листа"}
            print(f"{name:<26} — нет чтения")
            continue
        record = json.load(open(reading, encoding="utf-8"))
        gray = cv2.imread(str(image), 0)
        sheet = SheetReading(
            record["sheet_kind"], record["main"], [Region(**x) for x in record["regions"]]
        )
        result = choose_body(gray, sheet, record["labels"], {}, record["labels"])
        row = evaluate(part, result)
        rows[name] = row
        for key in totals:
            totals[key][0] += row[key][0]
            totals[key][1] += row[key][1]
        extra = row.get("refused") or f"L {row.get('length')} Ø {row.get('max_d')}"
        print(
            f"{name:<26} ступени {row['steps'][0]}/{row['steps'][1]}  пазы "
            f"{row['keyways'][0]}/{row['keyways'][1]}  попер. {row['cross_holes'][0]}/"
            f"{row['cross_holes'][1]}  {extra}"
        )
    print("ИТОГО", {k: f"{a}/{b}" for k, (a, b) in totals.items()})
    if args.out:
        json.dump(
            {"totals": totals, "parts": rows}, open(args.out, "w"), ensure_ascii=False, indent=1
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
