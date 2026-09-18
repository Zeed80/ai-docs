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
from typing import Any

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
    """Что обязан нести лист, чтобы по нему можно было изготовить деталь.

    Вал: диаметры ступеней и отверстия, длины ступеней кроме самой длинной
    (цепочка по ГОСТ 2.307 остаётся открытой) и габарит. Пластина и фланец:
    контур, толщина и диаметры отверстий. Координаты отверстий и окружность
    болтов сюда пока НЕ входят: их не проставляет и продукт (задача X1 плана) —
    метрика мерит то, что лист обязан нести уже сейчас, а не мечту.
    """
    body = spec["main_view"]
    profile = body.get("profile")
    if isinstance(profile, dict):
        diameters = {h["diameter_mm"] for h in profile.get("holes") or []}
        diameters |= {p["hole_diameter_mm"] for p in profile.get("hole_patterns") or []}
        # Окружность центров (PCD) — где стоят болтовые отверстия (X1).
        diameters |= {
            p["bolt_circle_diameter_mm"]
            for p in profile.get("hole_patterns") or []
            if p.get("kind", "bolt_circle") == "bolt_circle" and p.get("bolt_circle_diameter_mm")
        }
        lengths = [profile["thickness_mm"]]
        lengths += _wall_feature_values(profile, diameters)
        if profile.get("shape") == "circle":
            diameters.add(profile["diameter_mm"])
        else:
            lengths += [profile["width_mm"], profile["height_mm"]]
            lengths += _hole_coordinates(profile)
            if profile.get("corner_radius_mm"):
                lengths.append(profile["corner_radius_mm"])
            for slot in profile.get("slots") or []:
                # Межцентровое расстояние и радиус конца — как их ставит лист.
                lengths += [slot["length_mm"] - slot["width_mm"], slot["width_mm"] / 2.0]
        return {
            "diameters": sorted(diameters),
            "lengths": sorted(lengths, key=lambda v: v[0] if isinstance(v, tuple) else v),
            "overall": [],
        }
    outer = body["outer"]
    diameters = sorted(
        {s["diameter_mm"] for s in outer} | {s["diameter_mm"] for s in body.get("bore") or []}
    )
    lengths = [s["length_mm"] for s in outer]
    features = _shaft_feature_values(body, lengths)
    diameters = sorted(set(diameters) | set(features["diameters"]))
    # Открыта ровно ОДНА ступень — самая длинная. Список, не множество: у вала
    # shaft-1 две ступени по 80 и две по 15, лист не проставил обе «80» и
    # последнюю «15», а метрика по множеству значений засчитала лист полным.
    open_chain = sorted(lengths)[:-1]
    return {
        "diameters": diameters,
        "lengths": sorted(open_chain + features["lengths"]),
        "overall": [sum(lengths)],
    }


def _shaft_feature_values(body: dict, lengths: list[float]) -> dict[str, list[float]]:
    """Пазы и поперечные отверстия вала — как их ставит лист (Ф3.0b).

    Паз — длина и положение начала от левого уступа своей ступени, ширина и
    глубина на сечении; отверстие —
    Ø и положение центра от него же. Нулевое положение не ставится.
    """
    starts = [sum(lengths[:i]) for i in range(len(lengths))]

    def from_shoulder(x: float) -> float:
        return round(x - max((s for s in starts if s <= x + 1e-6), default=0.0), 3)

    values: dict[str, list[float]] = {"diameters": [], "lengths": []}
    for keyway in body.get("keyways") or []:
        values["lengths"].append(keyway["length_mm"])
        # Ширина b и глубина t1 — на вынесенном сечении через паз (X1b).
        values["lengths"] += [keyway["width_mm"], keyway["depth_mm"]]
        offset = from_shoulder(keyway["axial_start_mm"])
        if offset > 0.05:
            values["lengths"].append(offset)
    for hole in body.get("cross_holes") or []:
        values["diameters"].append(hole["diameter_mm"])
        offset = from_shoulder(hole["axial_position_mm"])
        if offset > 0.05:
            values["lengths"].append(offset)
    # Канавка (Ф3.0c) — ширина с глубиной в подписи «b×t»; одна её стенка —
    # уступ, место задано. Фаска на торце — «c×45°».
    for groove in body.get("grooves") or []:
        if not groove.get("internal"):
            values["lengths"].append(groove["width_mm"])
    for chamfer in body.get("chamfers") or []:
        if chamfer.get("location") in ("left_end", "right_end"):
            values["lengths"].append(chamfer["size_mm"])
    return values


def _hole_coordinates(profile: dict) -> list[float]:
    """Координаты центров отверстий пластины от левой и нижней кромки (X1).

    Одинаковые значения — один раз по каждой оси, как их ставит лист.
    """
    from app.ai.verify_corpus.score import expand_holes

    plain = {
        "holes": profile.get("holes") or [],
        "hole_patterns": [
            p for p in profile.get("hole_patterns") or [] if p.get("kind") != "bolt_circle"
        ],
    }
    xs: list[float] = []
    ys: list[float] = []
    centres = [(x, y) for x, y, _diameter in expand_holes(plain)]
    # Центр прорези ставится как центр отверстия.
    centres += [(slot["center_x_mm"], slot["center_y_mm"]) for slot in profile.get("slots") or []]
    for x, y in centres:
        x_from_left = round(x + profile["width_mm"] / 2.0, 3)
        y_from_bottom = round(y + profile["height_mm"] / 2.0, 3)
        if not any(abs(x_from_left - other) <= 0.05 for other in xs):
            xs.append(x_from_left)
        if not any(abs(y_from_bottom - other) <= 0.05 for other in ys):
            ys.append(y_from_bottom)
    return xs + ys


def _wall_feature_values(profile: dict, diameters: set[float]) -> list[float]:
    """Карманы и приливы на гранях корпуса (X2): размер, глубина, координаты.

    Изготовить элемент можно, только если лист несёт его размер, глубину (для
    прилива — вылет) и положение на своей грани. Координаты — от кромок тела на
    том же виде, как их ставит лист.
    """
    width = float(profile.get("width_mm") or 0.0)
    height = float(profile.get("height_mm") or 0.0)
    thickness = float(profile.get("thickness_mm") or 0.0)
    faces = {
        "top": (width, height),
        "bottom": (width, height),
        "front": (width, thickness),
        "back": (width, thickness),
        "left": (height, thickness),
        "right": (height, thickness),
    }
    values: list[float] = []
    for item in profile.get("wall_features") or []:
        face = faces.get(str(item.get("on_plane")))
        if not face:
            continue
        face_u, face_v = face
        values.append(float(item["depth_mm"]))
        if item.get("profile") == "rectangle":
            values += [float(item["width_mm"]), float(item["height_mm"])]
        else:
            diameters.add(float(item["diameter_mm"]))
        for centre, extent in (
            (float(item.get("center_u_mm") or 0.0), face_u),
            (float(item.get("center_v_mm") or 0.0), face_v),
        ):
            coordinate = round(extent / 2.0 + centre, 3)
            if coordinate > 0.05:
                # От любой из двух кромок грани: база — выбор конструктора
                # (ГОСТ 2.307), а оси вида ядра на разных гранях зеркальны.
                values.append((coordinate, round(extent - coordinate, 3)))
    return values


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

    def found(wanted: list[Any], shown: list[float]) -> int:
        """Значение засчитано, если оно есть на листе. Кортеж — равноправные
        варианты одного размера (координата от левой или от правой кромки)."""
        pool = list(shown)
        hits = 0
        for value in wanted:
            options = value if isinstance(value, tuple) else (value,)
            match = next(
                (s for s in pool if s is not None and any(abs(s - v) <= 0.05 for v in options)),
                None,
            )
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
