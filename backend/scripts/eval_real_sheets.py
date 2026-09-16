"""Реальные листы валов: цепочка проверки продукта без модели против эталона.

Для каждого листа из ``tests/fixtures/real_sheets_truth.json``: нормализованный
лист продукта (``<name>.png``) и чтение ридера ДО проверки (``<name>.spec.json``)
из ``cad-dataset-out/real-sheets`` проходят ту же цепочку, что ``cad_trace``:
проверка по листу → профиль по листу → повторная проверка → согласование →
пазы по листу. Итог — ступени и пазы спека против эталона.

Синтетический корпус (`verify_gate.py`) не видит того, что вскрыли реальные
листы: рамка листа и штамп проходят проверку торцов, увеличение рисует штрих
полым, зубчатый венец рвёт профиль. Правка поиска вида ради одного реального
листа ломала корпус — здесь видно обе стороны.

    PYTHONPATH=. python3 scripts/eval_real_sheets.py --sheets ../cad-dataset-out/real-sheets
    ... --baseline tests/fixtures/real_sheets_gate.json [--update]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TRUTH = (
    pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "real_sheets_truth.json"
)


def run_chain(png: bytes, spec: dict) -> dict:
    """Цепочка `cad_trace` без модели: проверка, профиль, согласование, пазы."""
    from app.ai.cad_recognize.verifiers.reconcile import (
        apply_contour,
        apply_keyway_additions,
        apply_profile,
        apply_reconciliation,
        apply_sleeve,
        contour_decision,
        keyway_additions,
        profile_decision,
        reconcile,
        sleeve_decision,
    )
    from app.ai.cad_recognize.verifiers.stage import verify_spec_against_sheet

    report = verify_spec_against_sheet(png, spec)
    adoption = profile_decision(spec, report)
    if adoption:
        spec = apply_profile(spec, adoption)
        report = verify_spec_against_sheet(png, spec)
        report["profile_adoption"] = adoption
    sleeve = sleeve_decision(spec, report)
    if sleeve:
        spec = apply_sleeve(spec, sleeve)
        report = verify_spec_against_sheet(png, spec)
        report["sleeve_adoption"] = sleeve
    contour = contour_decision(spec, report)
    if contour:
        spec = apply_contour(spec, contour)
        report = verify_spec_against_sheet(png, spec)
        report["contour_adoption"] = contour
    decisions = reconcile(spec, report)
    if decisions:
        spec, report = apply_reconciliation(spec, report, decisions)
    additions = keyway_additions(spec, report)
    if additions:
        spec = apply_keyway_additions(spec, additions)
    return {
        "spec": spec,
        "report": report,
        "profile_adopted": bool(adoption),
        "decisions": decisions,
        "additions": additions,
    }


def score(result: dict, truth: dict) -> dict:
    """Ступени и пазы итогового спека против эталона (значения, ±0,05 мм)."""
    main = result["spec"].get("main_view") or {}
    got = [
        (float(s.get("diameter_mm") or 0), float(s.get("length_mm") or 0))
        for s in main.get("outer") or []
    ]
    real = [(float(s["diameter_mm"]), float(s["length_mm"])) for s in truth["outer"]]
    steps_right = (
        sum(1 for a, b in zip(got, real) if abs(a[0] - b[0]) <= 0.05 and abs(a[1] - b[1]) <= 0.05)
        if len(got) == len(real)
        else 0
    )
    keys_got = [
        (
            float(k.get("axial_start_mm") or 0),
            float(k.get("length_mm") or 0),
            float(k.get("width_mm") or 0),
        )
        for k in main.get("keyways") or []
    ]
    keys_real = [
        (float(k["axial_start_mm"]), float(k["length_mm"]), float(k["width_mm"]))
        for k in truth.get("keyways") or []
    ]
    keys_right = sum(
        1 for k in keys_real if any(all(abs(a - b) <= 0.05 for a, b in zip(k, g)) for g in keys_got)
    )
    statuses = [
        i["status"] for i in result["report"].get("items") or [] if i["kind"] == "shaft_step"
    ]
    return {
        "steps": len(real),
        "steps_right": steps_right,
        "steps_confirmed": sum(1 for s in statuses if s == "confirmed"),
        "keyways": len(keys_real),
        "keyways_right": keys_right,
        "keyways_extra": max(0, len(keys_got) - keys_right),
        "profile_adopted": result["profile_adopted"],
    }


def score_flange(result: dict, truth: dict) -> dict:
    """Фланец: наружный и центральный Ø, окружность болтов (фаза по модулю шага)."""
    profile = (result["spec"].get("main_view") or {}).get("profile") or {}

    def near(value, target, tolerance=0.05):
        return isinstance(value, (int, float)) and abs(float(value) - float(target)) <= tolerance

    bore = [float(h.get("diameter_mm") or 0) for h in profile.get("holes") or []]
    patterns = [
        p
        for p in profile.get("hole_patterns") or []
        if (p.get("kind") or "bolt_circle") == "bolt_circle"
    ]
    want = truth["bolt_circle"]
    pattern_right = 0
    for pattern in patterns:
        count = int(pattern.get("count") or 0)
        step = 360.0 / count if count else 360.0
        phase = float(pattern.get("start_angle_deg") or 0.0)
        gap = abs((phase - want["start_angle_deg"] + step / 2) % step - step / 2)
        if (
            count == want["count"]
            and near(pattern.get("bolt_circle_diameter_mm"), want["bolt_circle_diameter_mm"])
            and near(pattern.get("hole_diameter_mm"), want["hole_diameter_mm"])
            and gap <= 0.5
        ):
            pattern_right = 1
    return {
        "outer_right": int(near(profile.get("diameter_mm"), truth["outer_diameter_mm"])),
        "bore_right": int(any(near(d, truth["bore_diameter_mm"]) for d in bore)),
        "pattern_right": pattern_right,
        "patterns_extra": max(0, len(patterns) - pattern_right),
    }


def score_plate(result: dict, truth: dict) -> dict:
    """Пластина: отверстия по Ø и центру от левой и нижней кромки (±0,5 мм).

    Центры спека прямоугольника — от середины пластины; эталон — от кромок.
    """
    profile = (result["spec"].get("main_view") or {}).get("profile") or {}
    # У прямоугольника центры — от середины; у эскиза — от его первой вершины,
    # левого нижнего угла (`plate_contour`).
    sketch = profile.get("shape") == "sketch"
    width = 0.0 if sketch else float(profile.get("width_mm") or 0.0)
    height = 0.0 if sketch else float(profile.get("height_mm") or 0.0)
    got = [
        (
            float(h.get("diameter_mm") or 0.0),
            float(h.get("center_x_mm") or 0.0) + width / 2.0,
            float(h.get("center_y_mm") or 0.0) + height / 2.0,
        )
        for h in profile.get("holes") or []
    ]
    right = sum(
        1
        for h in truth["holes"]
        if any(
            abs(d - h["diameter_mm"]) <= 0.05
            and abs(x - h["x_from_left_mm"]) <= 0.5
            and abs(y - h["y_from_bottom_mm"]) <= 0.5
            for d, x, y in got
        )
    )
    return {
        "holes": len(truth["holes"]),
        "holes_right": right,
        "holes_extra": max(0, len(got) - right),
        "thickness_right": int(
            abs(float(profile.get("thickness_mm") or 0.0) - truth["thickness_mm"]) <= 0.05
        ),
    }


def _steps_right(got_sections: list[dict], real: list[dict]) -> int:
    got = [(float(s.get("diameter_mm") or 0), float(s.get("length_mm") or 0)) for s in got_sections]
    want = [(float(s["diameter_mm"]), float(s["length_mm"])) for s in real]
    if len(got) != len(want):
        return 0
    return sum(
        1 for a, b in zip(got, want) if abs(a[0] - b[0]) <= 0.05 and abs(a[1] - b[1]) <= 0.05
    )


def _outline_iou(sketch: list, origin: tuple[float, float], truth: dict) -> float:
    """IoU контура фланца спека и эталона — растром 0,05 мм в системе оси."""
    import numpy as np
    from PIL import Image, ImageDraw

    from app.ai.cad_dimension_graph import sketch_outline
    from app.ai.cad_recognize.verifiers.flange_outline import outline_sketch

    want_sketch, want_origin = outline_sketch(
        truth["diameter_mm"] / 2.0, truth["flats"], truth["flat_distance_mm"], truth["phase_deg"]
    )

    def mask(segments, start):
        polygon = sketch_outline(segments, step_deg=1.0)
        if not polygon:
            return None
        size, cell = 800, 0.05
        image = Image.new("1", (size, size), 0)
        ImageDraw.Draw(image).polygon(
            [
                (size / 2 + (x + start[0]) / cell, size / 2 - (y + start[1]) / cell)
                for x, y in polygon
            ],
            fill=1,
        )
        return np.asarray(image)

    got, want = mask(sketch, origin), mask(want_sketch, want_origin)
    if got is None or want is None:
        return 0.0
    return float((got & want).sum() / max(1, (got | want).sum()))


def score_sleeve(result: dict, truth: dict) -> dict:
    """Втулка с фланцем: ступени, расточка, фланец (станция, толщина, контур) и
    отверстия фланца (положения ±0,2 мм)."""
    from app.ai.cad_recognize.spec_vectorize import _expanded_profile_holes

    main = result["spec"].get("main_view") or {}
    scores = {
        "steps": len(truth["outer"]),
        "steps_right": _steps_right(main.get("outer") or [], truth["outer"]),
        "bore_right": _steps_right(main.get("bore") or [], truth["bore"]),
    }
    groove = truth.get("face_groove")
    if groove:
        scores["face_groove_right"] = int(
            any(
                g.get("end", "left") == groove["end"]
                and all(
                    abs(float(g.get(key) or 0) - groove[key]) <= 0.05
                    for key in ("depth_mm", "outer_diameter_mm", "inner_diameter_mm")
                )
                for g in main.get("face_grooves") or []
            )
        )
    want = truth.get("flange")
    if not want:
        return scores
    flange_right = holes_right = 0
    for flange in main.get("flanges") or []:
        profile = flange.get("profile") or {}
        if not (
            abs(float(flange.get("axial_start_mm") or -1) - want["axial_start_mm"]) <= 0.05
            and abs(float(flange.get("thickness_mm") or 0) - want["thickness_mm"]) <= 0.05
        ):
            continue
        origin = tuple(flange.get("sketch_origin_mm") or (0.0, 0.0))
        if (
            profile.get("shape") == "sketch"
            and _outline_iou(profile.get("sketch") or [], origin, want) >= 0.98
        ):
            flange_right = 1
        import math

        pattern = want["holes"]
        holes = _expanded_profile_holes(profile) or []
        targets = [
            (
                pattern["bolt_circle_diameter_mm"]
                / 2
                * math.cos(math.radians(pattern["start_angle_deg"] + k * 360 / pattern["count"])),
                pattern["bolt_circle_diameter_mm"]
                / 2
                * math.sin(math.radians(pattern["start_angle_deg"] + k * 360 / pattern["count"])),
            )
            for k in range(pattern["count"])
        ]
        holes_right = max(
            holes_right,
            sum(
                1
                for tx, ty in targets
                if any(
                    abs(float(h.get("diameter_mm") or 0) - pattern["hole_diameter_mm"]) <= 0.05
                    and math.hypot(
                        float(h.get("center_x_mm") or 0) - tx, float(h.get("center_y_mm") or 0) - ty
                    )
                    <= 0.2
                    for h in holes
                )
            ),
        )
    return {**scores, "flange_right": flange_right, "flange_holes_right": holes_right}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sheets", type=pathlib.Path, required=True)
    parser.add_argument("--baseline", type=pathlib.Path)
    parser.add_argument("--update", action="store_true")
    args = parser.parse_args()
    truth = json.loads(TRUTH.read_text())
    results = {}
    for sheet in truth["sheets"]:
        kind = sheet.get("kind", "shaft")
        if kind not in {"shaft", "flange", "plate", "sleeve_flange"}:
            print(f"{sheet['name']}: {kind} — оценка пока не написана, пропущен")
            continue
        png_path = args.sheets / f"{sheet['name']}.png"
        spec_path = args.sheets / f"{sheet['name']}.spec.json"
        if not png_path.exists() or not spec_path.exists():
            print(f"{sheet['name']}: нет листа или чтения в {args.sheets} — пропущен")
            continue
        result = run_chain(png_path.read_bytes(), json.loads(spec_path.read_text()))
        if kind == "plate":
            results[sheet["name"]] = score_plate(result, sheet)
            f = results[sheet["name"]]
            print(
                f"{sheet['name']:<18} отверстия {f['holes_right']}/{f['holes']} верно "
                f"(лишних {f['holes_extra']}), толщина {f['thickness_right']}"
            )
            continue
        if kind == "sleeve_flange":
            results[sheet["name"]] = score_sleeve(result, sheet)
            f = results[sheet["name"]]
            extra = (
                f", фланец {f['flange_right']}, отверстия фланца "
                f"{f['flange_holes_right']}/{sheet['flange']['holes']['count']}"
                if "flange" in sheet
                else ""
            ) + (f", выточка на торце {f['face_groove_right']}" if "face_groove" in sheet else "")
            print(
                f"{sheet['name']:<18} ступени {f['steps_right']}/{f['steps']}, расточка "
                f"{f['bore_right']}/{len(sheet['bore'])}{extra}"
            )
            continue
        if kind == "flange":
            results[sheet["name"]] = score_flange(result, sheet)
            f = results[sheet["name"]]
            print(
                f"{sheet['name']:<18} наружный Ø {f['outer_right']}, центральное {f['bore_right']}, "
                f"окружность болтов {f['pattern_right']} (лишних {f['patterns_extra']})"
            )
            continue
        results[sheet["name"]] = score(result, sheet)
        s = results[sheet["name"]]
        print(
            f"{sheet['name']:<18} ступени {s['steps_right']}/{s['steps']} верно "
            f"(подтверждено {s['steps_confirmed']}), пазы {s['keyways_right']}/{s['keyways']} "
            f"(лишних {s['keyways_extra']}), профиль по листу: {'да' if s['profile_adopted'] else 'нет'}"
        )
    if not args.baseline:
        return 0
    if args.update:
        args.baseline.write_text(
            json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        )
        print(f"база записана: {args.baseline}")
        return 0
    base = json.loads(args.baseline.read_text())
    worse = []
    for name, got in results.items():
        was = base.get(name)
        if was is None:
            continue
        for key in (
            "steps_right",
            "keyways_right",
            "outer_right",
            "bore_right",
            "pattern_right",
            "holes_right",
            "thickness_right",
            "flange_right",
            "flange_holes_right",
            "face_groove_right",
        ):
            if key in got and got[key] < was.get(key, 0):
                worse.append(f"{name}.{key}: {got[key]} < {was[key]}")
        for key in ("keyways_extra", "patterns_extra", "holes_extra"):
            if key in got and got[key] > was.get(key, 0):
                worse.append(f"{name}.{key}: {got[key]} > {was[key]}")
    for line in worse:
        print("ХУЖЕ:", line)
    print("гейт реальных листов", "не пройден" if worse else "пройден")
    return 1 if worse else 0


if __name__ == "__main__":
    raise SystemExit(main())
