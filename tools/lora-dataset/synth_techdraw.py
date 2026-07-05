#!/usr/bin/env python3
"""Generate synthetic ЕСКД drawings (random shafts / plates / flanges /
assemblies) as clean PNG targets for the cleanup-LoRA dataset, using the
project's own deterministic renderer (backend/app/ai/techdraw.py).

Unlike the DWG renders these are unlimited in count and carry NO confidential
content — a dataset built purely from synthetics can be trained on rented
cloud GPUs without leaking anything (real企业 DWGs stay local-only per the
project's Dual-AI rule).

``show_frame=True``: photos in the wild are of printed sheets with the ГОСТ
frame and title block, so targets get them too.

Usage:
    python3 synth_techdraw.py --count 50 --out <png dir> [--seed 0]
                              [--repo <repo root>]
"""

from __future__ import annotations

import argparse
import io
import pathlib
import random
import sys

_NAMES_SHAFT = ["Вал", "Вал-шестерня", "Ось", "Валик", "Шток", "Палец"]
_NAMES_PLATE_RECT = ["Планка", "Пластина", "Крышка", "Плита опорная"]
_NAMES_PLATE_CIRCLE = ["Фланец", "Диск", "Кольцо опорное", "Крышка торцевая"]
_NAMES_ASM = ["Узел натяжения", "Опора", "Кронштейн в сборе", "Ролик в сборе"]
_MATERIALS = [
    "Сталь 45 ГОСТ 1050-88", "Сталь 40Х ГОСТ 4543-71", "Сталь 20 ГОСТ 1050-88",
    "Бр.АМц 9-2 ГОСТ 18175-78", "Д16Т ГОСТ 4784-97", "СЧ20 ГОСТ 1412-85",
]
_TOLS_SHAFT = ["", "h6", "h7", "k6", "js6", "f7"]
_TOLS_HOLE = ["", "H7", "H8"]
_THREADS = ["M12", "M16", "M20×1.5", "M24×2", "M30×2"]
_RA = [None, 0.8, 1.6, 3.2, 6.3]


def _title(rng: random.Random, name_pool: list[str]) -> dict:
    return {
        "name": rng.choice(name_pool),
        "designation": f"ТМ.{rng.randint(100000, 999999)}.{rng.randint(1, 999):03d}",
        "material": rng.choice(_MATERIALS),
        "developer": rng.choice(["Иванов", "Петров", "Сидорова", "Кузнецов"]),
        "checked_by": rng.choice(["Смирнов", "Волкова", ""]),
        "litera": rng.choice(["", "У", "О1"]),
        "show_frame": True,
    }


def _shaft_spec(rng: random.Random) -> dict:
    n = rng.randint(2, 6)
    segments = []
    for i in range(n):
        d = rng.choice([12, 16, 20, 25, 30, 35, 40, 45, 50, 60])
        seg = {
            "diameter": float(d),
            "length": float(rng.choice([15, 20, 25, 30, 40, 50, 60, 80])),
            "tolerance": rng.choice(_TOLS_SHAFT),
            "roughness": rng.choice(_RA),
            "chamfer": rng.choice([0.0, 0.0, 1.0, 1.6, 2.0]),
        }
        if i in (0, n - 1) and rng.random() < 0.35:
            seg["thread"] = rng.choice(_THREADS)
            seg["thread_end_view"] = rng.random() < 0.5
        if rng.random() < 0.2:
            seg["bore_diameter"] = float(max(4, d - rng.choice([6, 8, 10])))
            seg["section_hatch"] = True
        segments.append(seg)
    return {"type": "shaft", "segments": segments, "title": _title(rng, _NAMES_SHAFT)}


def _plate_spec(rng: random.Random) -> dict:
    shape = rng.choice(["rect", "circle"])
    spec: dict = {
        "type": "plate",
        "shape": shape,
        "thickness": float(rng.choice([6, 8, 10, 12, 16, 20])),
        "thickness_tol": rng.choice(["", "h14", "js14"]),
        "roughness": rng.choice(_RA),
        "title": _title(rng, _NAMES_PLATE_RECT if shape == "rect" else _NAMES_PLATE_CIRCLE),
        "holes": [],
    }
    if shape == "rect":
        spec["width"] = float(rng.choice([60, 80, 100, 120, 160]))
        spec["height"] = float(rng.choice([40, 60, 80, 100]))
        for _ in range(rng.randint(0, 4)):
            spec["holes"].append({
                "x": rng.uniform(-0.35, 0.35) * spec["width"],
                "y": rng.uniform(-0.35, 0.35) * spec["height"],
                "diameter": float(rng.choice([6, 8, 10, 12])),
                "tolerance": rng.choice(_TOLS_HOLE),
            })
    else:
        spec["diameter"] = float(rng.choice([80, 100, 120, 160, 200]))
        if rng.random() < 0.7:
            spec["bolt_circle_d"] = spec["diameter"] * rng.uniform(0.6, 0.8)
            spec["bolt_circle_n"] = rng.choice([4, 6, 8])
            spec["bolt_hole_d"] = float(rng.choice([6, 9, 11, 13]))
            spec["bolt_hole_tol"] = rng.choice(_TOLS_HOLE)
        if rng.random() < 0.5:
            spec["holes"].append({
                "x": 0.0, "y": 0.0,
                "diameter": spec["diameter"] * rng.uniform(0.2, 0.4),
                "tolerance": "H7",
            })
    return spec


def _assembly_spec(rng: random.Random) -> dict:
    parts = []
    bom = []
    for pos in range(1, rng.randint(2, 4) + 1):
        child = _shaft_spec(rng) if rng.random() < 0.5 else _plate_spec(rng)
        child["title"]["show_frame"] = False
        parts.append({"ref": str(pos), "spec": child, "qty": rng.randint(1, 4)})
        bom.append({
            "pos": pos,
            "designation": child["title"]["designation"],
            "name": child["title"]["name"],
            "qty": parts[-1]["qty"],
            "material": child["title"]["material"],
        })
    title = _title(rng, _NAMES_ASM)
    title["material"] = ""
    return {"type": "assembly", "components": parts, "bom": bom, "title": title}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--out", required=True, type=pathlib.Path)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--long-side", type=int, default=2048)
    ap.add_argument("--repo", type=pathlib.Path,
                    default=pathlib.Path(__file__).resolve().parents[2])
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(args.repo / "backend"))
    import cairosvg  # noqa: E402
    from PIL import Image  # noqa: E402

    from app.ai.techdraw import render_spec_to_svg  # noqa: E402

    rng = random.Random(args.seed)
    ok = fail = 0
    for i in range(args.count):
        kind = rng.choices(["shaft", "plate", "assembly"], weights=[4, 4, 2])[0]
        spec = {"shaft": _shaft_spec, "plate": _plate_spec, "assembly": _assembly_spec}[kind](rng)
        try:
            svg = render_spec_to_svg(spec)
            png = cairosvg.svg2png(bytestring=svg.encode(), output_width=args.long_side,
                                   background_color="white")
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img.save(args.out / f"synth_{kind}_{i:04d}.png")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {kind} #{i}: {str(exc)[:120]}", file=sys.stderr)
            fail += 1
    print(f"done: {ok} synthetic targets, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
