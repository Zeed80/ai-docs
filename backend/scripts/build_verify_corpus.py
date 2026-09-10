#!/usr/bin/env python3
"""Собрать партию синтетического корпуса с эталоном для проверяльщиков.

Запуск внутри контейнера backend (нужен cad-kernel):

    python scripts/build_verify_corpus.py --kind shaft --seeds 0:40 --dpi 300 --out /tmp/verify-corpus

На выходе на каждый лист: ``<kind>-<seed>.png`` и ``<kind>-<seed>.json`` (эталон),
плюс ``manifest.jsonl`` и сводка полноты простановки размеров. Split dev/holdout
решается по seed детерминированно — holdout не плывёт между прогонами.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.ai.verify_corpus.build import SampleUnavailable, build_sample  # noqa: E402
from app.ai.verify_corpus.synth import synth_spec  # noqa: E402

# Каждый пятый лист — holdout. По хэшу, а не по чётности seed: иначе в
# holdout попадали бы систематически похожие детали.
_HOLDOUT_EVERY = 5


def split_for(kind: str, seed: int) -> str:
    digest = hashlib.sha256(f"{kind}:{seed}".encode()).digest()
    return "holdout" if digest[0] % _HOLDOUT_EVERY == 0 else "dev"


def needed_dimensions(spec: dict) -> dict[str, list[float]]:
    """Что обязан нести лист вала, чтобы по нему можно было изготовить деталь.

    Диаметры всех ступеней и отверстия, длины всех ступеней кроме самой длинной
    (цепочка по ГОСТ 2.307 остаётся открытой) и габарит.
    """
    body = spec["main_view"]
    outer = body["outer"]
    diameters = sorted(
        {s["diameter_mm"] for s in outer} | {s["diameter_mm"] for s in body.get("bore") or []}
    )
    lengths = [s["length_mm"] for s in outer]
    longest = max(lengths)
    open_chain = sorted({value for value in lengths if value != longest})
    return {"diameters": diameters, "lengths": open_chain, "overall": [sum(lengths)]}


def coverage(spec: dict, truth: dict) -> dict[str, float]:
    needed = needed_dimensions(spec)
    shown_d = [
        d["value_mm"]
        for d in truth["labels"]
        if d["kind"] == "dimension" and d["dimension_kind"] == "diameter"
    ]
    shown_l = [
        d["value_mm"]
        for d in truth["labels"]
        if d["kind"] == "dimension" and d["dimension_kind"] != "diameter"
    ]

    def found(wanted: list[float], shown: list[float]) -> int:
        pool = list(shown)
        hits = 0
        for value in wanted:
            match = next((s for s in pool if s is not None and abs(s - value) <= 0.05), None)
            if match is not None:
                pool.remove(match)
                hits += 1
        return hits

    total = sum(len(v) for v in needed.values())
    hits = found(needed["diameters"], shown_d) + found(
        needed["lengths"] + needed["overall"], shown_l
    )
    return {"needed": total, "shown": hits, "ratio": hits / total if total else 1.0}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", default="shaft")
    parser.add_argument("--seeds", default="0:20", help="диапазон a:b")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    first, last = (int(part) for part in args.seeds.split(":"))
    args.out.mkdir(parents=True, exist_ok=True)

    built = failed = 0
    ratios: list[float] = []
    with (args.out / "manifest.jsonl").open("a", encoding="utf-8") as manifest:
        for seed in range(first, last):
            name = f"{args.kind}-{seed}"
            spec = synth_spec(args.kind, seed)
            started = time.monotonic()
            try:
                sample = await build_sample(spec, dpi=args.dpi)
            except (SampleUnavailable, Exception) as exc:  # noqa: BLE001 — одна деталь, не партия
                failed += 1
                manifest.write(
                    json.dumps(
                        {"name": name, "status": "failed", "error": str(exc)[:300]},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                continue
            cov = coverage(spec, sample.truth)
            ratios.append(cov["ratio"])
            (args.out / f"{name}.png").write_bytes(sample.png)
            (args.out / f"{name}.json").write_text(
                json.dumps(sample.truth, ensure_ascii=False, default=str), encoding="utf-8"
            )
            manifest.write(
                json.dumps(
                    {
                        "name": name,
                        "kind": args.kind,
                        "seed": seed,
                        "split": split_for(args.kind, seed),
                        "dpi": args.dpi,
                        "status": "built",
                        "coverage": cov,
                        "seconds": round(time.monotonic() - started, 1),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            built += 1

    full = sum(1 for value in ratios if value >= 0.999)
    mean = sum(ratios) / len(ratios) if ratios else 0.0
    print(
        f"собрано {built}, не собрано {failed}; полнота размеров: средняя {mean:.2f}, полных листов {full}/{built}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
