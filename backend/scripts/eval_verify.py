#!/usr/bin/env python3
"""Харнесс проверяльщиков на корпусе с эталоном (план, задача M1).

Детерминированный режим: проверяльщику подаются ИСТИННЫЕ гипотезы из эталона —
без модели. Так мерится сам проверяльщик, отдельно от того, насколько точно
модель даёт рамки (это эксперимент E0, живой режим). По ступеням лестницы
деградации выходит кривая — это эксперимент E2: порог измеримости.

    python scripts/eval_verify.py --corpus ../cad-dataset-out/verify-corpus-v1-ladder \\
        --verifier dimension_line --split dev

Проверяльщики:
* ``dimension_line`` — длина горизонтальной размерной линии по чернилам рядом с
  подписью (`axial_dimensions._span_from_ink`). Истина — расстояние между
  точками привязки размера по горизонтали.
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import statistics
import sys
from collections import defaultdict
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def product_normalize(png: bytes, truth: dict) -> tuple[bytes, dict, bool]:
    """Подготовить лист так же, как стадия 0.9 продукта (`cad_trace._dewarp_photo`).

    Проверяльщик в продукте видит фото уже выпрямленным — харнесс обязан мерить
    то же самое, иначе он оценивает не продукт. Эталон переносится той же
    гомографией. Для скана выпрямление не срабатывает и лист не меняется.
    """
    import copy

    import numpy as np
    from PIL import Image

    from app.ai.drawing_cleanup import dewarp_sheet_with_transform
    from app.ai.verify_corpus.degrade import _apply_homography, _encode, _map_points

    arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    result = dewarp_sheet_with_transform(arr)
    if result is None:
        return png, truth, False
    warped, homography = result
    moved = copy.deepcopy(truth)
    _map_points(moved, lambda points: _apply_homography(np.asarray(homography), points))
    moved["image_size_px"] = [int(warped.shape[1]), int(warped.shape[0])]
    return _encode(warped), moved, True


def _horizontal_dimensions(truth: dict) -> list[dict[str, Any]]:
    """Линейные размеры с горизонтальной размерной линией и подписью."""
    result = []
    for item in truth.get("labels") or []:
        if item.get("kind") != "dimension" or item.get("dimension_kind") != "linear":
            continue
        label = item.get("label") or {}
        anchors = item.get("anchors_px") or []
        if not label.get("bbox_px") or len(anchors) != 2:
            continue
        (x1, y1), (x2, y2) = anchors
        # Вертикальные размеры (высота пластины и т. п.) сюда не идут: этот
        # проверяльщик сканирует строки и вертикальную линию не ищет по
        # устройству. Первая версия харнесса подавала их ему и засчитывала
        # провалом — 54 «ненайденных» из 216 были ошибкой харнесса.
        if abs(x2 - x1) < abs(y2 - y1):
            continue
        # Размерная линия длины горизонтальна по построению листа, даже если
        # точки привязки на разной высоте; на фото она наклонена перспективой.
        result.append({"label_bbox": label["bbox_px"], "span_px": abs(x2 - x1), "dy": abs(y2 - y1)})
    return result


def eval_dimension_line(png: bytes, truth: dict) -> list[dict[str, Any]]:
    from PIL import Image

    from app.ai.cad_recognize.axial_dimensions import _ink_rows, _span_from_ink

    image = Image.open(io.BytesIO(png)).convert("RGB")
    ink = _ink_rows(image)
    outcomes = []
    for dim in _horizontal_dimensions(truth):
        x0, y0, x1, y1 = dim["label_bbox"]
        unit = max(4.0, y1 - y0)
        line = _span_from_ink(ink, [x0, y0, x1, y1], unit)
        expected = float(dim["span_px"])
        if line is None:
            outcomes.append({"found": False, "expected_px": expected, "unit_px": unit})
            continue
        measured = float(line[2] - line[0])
        error = abs(measured - expected)
        tolerance = max(2.0, 0.02 * expected)
        outcomes.append(
            {
                "found": True,
                "correct": error <= tolerance,
                "expected_px": expected,
                "measured_px": measured,
                "error_px": error,
                "error_rel": error / expected if expected else None,
                "unit_px": unit,
            }
        )
    return outcomes


def _plate_frame(truth: dict):
    """Система координат плана пластины — по эталонным размерам ширины и высоты.

    Точки размеров в эталоне лежат на размерных линиях: у ширины это левая и
    правая кромки плана по x, у высоты — нижняя и верхняя по y.
    """
    from app.ai.cad_recognize.verifiers import ViewFrame

    profile = (truth["spec"].get("main_view") or {}).get("profile") or {}
    if profile.get("shape") != "rectangle":
        return None
    width, height = float(profile["width_mm"]), float(profile["height_mm"])
    width_dim = height_dim = None
    for item in truth.get("labels") or []:
        if item.get("kind") != "dimension" or item.get("dimension_kind") != "linear":
            continue
        anchors = item.get("anchors_px") or []
        if len(anchors) != 2 or item.get("value_mm") is None:
            continue
        (x1, y1), (x2, y2) = anchors
        horizontal = abs(x2 - x1) >= abs(y2 - y1)
        if horizontal and width_dim is None and abs(item["value_mm"] - width) <= 0.05:
            width_dim = (min(x1, x2), max(x1, x2))
        if not horizontal and height_dim is None and abs(item["value_mm"] - height) <= 0.05:
            height_dim = (min(y1, y2), max(y1, y2))
    if width_dim is None or height_dim is None:
        return None
    # Масштаб по каждой оси своим размером: выпрямленное фото не изотропно,
    # и масштаб одной ширины уводил верх плана на 16 px (plate-3@photo).
    return ViewFrame(
        bbox_px=(width_dim[0] - 5, height_dim[0] - 5, width_dim[1] + 5, height_dim[1] + 5),
        mm_per_px=width / (width_dim[1] - width_dim[0]),
        origin_px=(width_dim[0], height_dim[1]),
        mm_per_px_v=height / (height_dim[1] - height_dim[0]),
    )


def eval_plate_hole(png: bytes, truth: dict, frame_source: str = "truth") -> list[dict[str, Any]]:
    """Отверстия пластины: гипотезы из эталона и искажённые, как ошибается ридер.

    ``truth`` — должно подтвердиться; ``swap_y`` — y от соседнего отверстия
    (plate-1: все x верны, y переставлены парами) и ``diameter`` — Ø на 1,1 мм
    больше: оба должны опровергнуться, а измеренное — совпасть с эталоном.
    """
    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis, verify
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances
    from app.ai.verify_corpus.score import expand_holes

    reference = _plate_frame(truth)
    if reference is None:
        return []
    profile = truth["spec"]["main_view"]["profile"]
    width, height = float(profile["width_mm"]), float(profile["height_mm"])
    holes = [(x + width / 2.0, y + height / 2.0, d) for x, y, d in expand_holes(profile)]
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    frame, frame_error = reference, None
    if frame_source == "sheet":
        # Как в продукте: ширину и высоту дал ридер (на пластинах — 100 %),
        # план ищется по листу. Эталонная система — только для оценки.
        from app.ai.cad_recognize.verifiers.plate_frame import locate_plate_frame

        frame = locate_plate_frame(gray, width, height)
        if frame is not None:
            frame_error = {
                "scale_rel": abs(frame.mm_per_px / reference.mm_per_px - 1.0),
                "origin_px": max(
                    abs(frame.origin_px[0] - reference.origin_px[0]),
                    abs(frame.origin_px[1] - reference.origin_px[1]),
                ),
            }
    cases: list[tuple[str, tuple[float, float, float], tuple[float, float, float]]] = []
    for index, hole in enumerate(holes):
        cases.append(("truth", hole, hole))
        # Перестановка y — как у plate-1: y соседнего отверстия из ДРУГОГО
        # столбца, но такой, на котором в столбце этого отверстия отверстия нет.
        # Иначе (массив 2×2) гипотеза описывала существующее отверстие и
        # законно подтверждалась — первый прогон v7 считал это промахом.
        column = [h for h in holes if abs(h[0] - hole[0]) <= 1.0]
        other = next(
            (
                h
                for h in holes[index + 1 :] + holes[:index]
                if abs(h[0] - hole[0]) > h[2] + hole[2]
                and all(abs(h[1] - c[1]) > 2.0 for c in column)
            ),
            None,
        )
        if other is not None:
            # Верный замер — отверстие столбца, ближайшее к заявленному y: так
            # проверяльщик и устроен (x выбирает столбец, y — ближайшее в нём).
            nearest = min(column, key=lambda c: abs(c[1] - other[1]))
            cases.append(("swap_y", (hole[0], other[1], hole[2]), nearest))
        cases.append(("diameter", (hole[0], hole[1], hole[2] + 1.1), hole))
    outcomes = []
    for case, (x, y, d), real in cases:
        verdict = verify(
            Hypothesis("plate_hole", "holes", {"x_mm": x, "y_mm": y, "diameter_mm": d}),
            frame,
            gray,
        )
        measured = verdict.measured
        position_tol, diameter_tol = plate_hole_tolerances(reference.scale_mean)
        accurate = bool(measured) and (
            abs(measured["y_mm"] - real[1]) <= position_tol
            and abs(measured["diameter_mm"] - real[2]) <= diameter_tol
        )

        wanted = "confirmed" if case == "truth" else "refuted"
        outcomes.append(
            {
                "case": case,
                "found": verdict.status != "unmeasurable",
                "correct": verdict.status == wanted and accurate,
                "error_rel": (
                    abs(measured["diameter_mm"] - real[2]) / real[2] if measured else None
                ),
                "unit_px": real[2] / 2.0 / reference.scale_mean,
                "frame_found": frame is not None,
                "frame_error": frame_error,
            }
        )
    return outcomes


def eval_bolt_circle(png: bytes, truth: dict) -> list[dict[str, Any]]:
    """Окружность болтов фланца: система координат — по контуру на листе.

    Случаи: ``truth`` — должно подтвердиться; ``count`` (+2 отверстия),
    ``pcd`` (+6 мм), ``diameter`` (Ø +1,1 мм) и ``phase_zero`` — фаза 0°, когда
    настоящая не кратна шагу (так ошибается ридер: углового размера на листе
    нет) — должны опровергнуться, а измеренное — совпасть с эталоном.
    Точность системы координат — против середины точек размера наружного Ø.
    """
    import math

    import numpy as np
    from PIL import Image

    from app.ai.cad_recognize.verifiers import Hypothesis, verify
    from app.ai.cad_recognize.verifiers.bolt_circle import _angle_gap
    from app.ai.cad_recognize.verifiers.circle_frame import locate_circle_frame
    from app.ai.cad_recognize.verifiers.plate_hole import plate_hole_tolerances

    profile = (truth["spec"].get("main_view") or {}).get("profile") or {}
    patterns = [
        item
        for item in profile.get("hole_patterns") or []
        if (item.get("kind") or "bolt_circle") == "bolt_circle" and item.get("count")
    ]
    if profile.get("shape") != "circle" or not patterns or not profile.get("diameter_mm"):
        return []
    outer_mm = float(profile["diameter_mm"])
    outer = next(
        (
            item
            for item in truth.get("labels") or []
            if item.get("kind") == "dimension"
            and item.get("dimension_kind") == "diameter"
            and item.get("value_mm") is not None
            and abs(item["value_mm"] - outer_mm) <= 0.05
            and len(item.get("anchors_px") or []) == 2
        ),
        None,
    )
    if outer is None:
        return []
    (ax, ay), (bx, by) = outer["anchors_px"]
    ref_scale = outer_mm / math.hypot(bx - ax, by - ay)
    gray = np.asarray(Image.open(io.BytesIO(png)).convert("L"))
    frame = locate_circle_frame(gray, outer_mm)
    frame_error = None
    if frame is not None:
        frame_error = {
            "scale_rel": abs(frame.mm_per_px / ref_scale - 1.0),
            "origin_px": max(
                abs(frame.origin_px[0] - (ax + bx) / 2.0),
                abs(frame.origin_px[1] - (ay + by) / 2.0),
            ),
        }
    position_tol, diameter_tol = plate_hole_tolerances(ref_scale)
    outcomes = []
    for pattern in patterns:
        count = int(pattern["count"])
        pcd = float(pattern["bolt_circle_diameter_mm"])
        hole = float(pattern["hole_diameter_mm"])
        step = 360.0 / count
        phase = float(pattern.get("start_angle_deg") or 0.0) % step
        read = {"count": count, "pcd_mm": pcd, "hole_diameter_mm": hole, "start_angle_deg": phase}
        cases = [
            ("truth", read),
            ("count", {**read, "count": count + 2}),
            ("pcd", {**read, "pcd_mm": pcd + 6.0}),
            ("diameter", {**read, "hole_diameter_mm": hole + 1.1}),
        ]
        if _angle_gap(phase, 0.0, step) > 5.0:
            cases.append(("phase_zero", {**read, "start_angle_deg": 0.0}))
        for case, expected in cases:
            verdict = verify(Hypothesis("bolt_circle", "hole_patterns", expected), frame, gray)
            measured = verdict.measured
            accurate = bool(measured) and (
                measured["count"] == count
                and abs(measured["pcd_mm"] - pcd) <= 2.0 * position_tol
                and abs(measured["hole_diameter_mm"] - hole) <= diameter_tol
                and _angle_gap(measured["start_angle_deg"], phase, step) <= 2.0
            )
            wanted = "confirmed" if case == "truth" else "refuted"
            outcomes.append(
                {
                    "case": case,
                    "found": verdict.status != "unmeasurable",
                    "correct": verdict.status == wanted and accurate,
                    "error_rel": (abs(measured["pcd_mm"] - pcd) / pcd if measured else None),
                    "unit_px": hole / 2.0 / ref_scale,
                    "frame_found": frame is not None,
                    "frame_error": frame_error,
                }
            )
    return outcomes


_VERIFIERS = {
    "bolt_circle": eval_bolt_circle,
    "dimension_line": eval_dimension_line,
    "plate_hole": eval_plate_hole,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--verifier", choices=sorted(_VERIFIERS), required=True)
    parser.add_argument("--split", default="dev", choices=("dev", "holdout", "all"))
    parser.add_argument("--report", type=pathlib.Path)
    parser.add_argument(
        "--raw", action="store_true", help="без нормализации продукта (выпрямления фото)"
    )
    parser.add_argument(
        "--min-correct",
        type=float,
        help="гейт: код 1, если доля верных на какой-либо ступени ниже порога",
    )
    parser.add_argument(
        "--frame",
        default="truth",
        choices=("truth", "sheet"),
        help="plate_hole: система координат плана из эталона или найденная по листу",
    )
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in (args.corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evaluate = _VERIFIERS[args.verifier]
    if args.verifier == "plate_hole":
        evaluate = lambda png, truth: eval_plate_hole(png, truth, args.frame)  # noqa: E731
    by_step: dict[str, list[dict]] = defaultdict(list)
    by_kind_step: dict[tuple[str, str], list[dict]] = defaultdict(list)
    dewarp_count: dict[str, int] = defaultdict(int)
    for row in rows:
        if args.split != "all" and row.get("split") != args.split:
            continue
        png = (args.corpus / f"{row['name']}.png").read_bytes()
        truth = json.loads((args.corpus / f"{row['name']}.json").read_text(encoding="utf-8"))
        if not args.raw:
            png, truth, dewarped = product_normalize(png, truth)
            dewarp_count[row["step"]] += int(dewarped)
        outcomes = evaluate(png, truth)
        by_step[row["step"]].extend(outcomes)
        by_kind_step[(row.get("kind") or "?", row["step"])].extend(outcomes)

    def summary(outcomes: list[dict]) -> dict[str, Any]:
        found = [item for item in outcomes if item["found"]]
        correct = [item for item in found if item.get("correct")]
        errors = [item["error_rel"] for item in found if item.get("error_rel") is not None]
        units = [item["unit_px"] for item in outcomes]
        return {
            "n": len(outcomes),
            "found": len(found) / len(outcomes) if outcomes else None,
            "correct": len(correct) / len(outcomes) if outcomes else None,
            "median_error_rel": statistics.median(errors) if errors else None,
            "median_text_px": statistics.median(units) if units else None,
        }

    report = {
        "verifier": args.verifier,
        "split": args.split,
        "normalized": not args.raw,
        "dewarped_by_step": dict(dewarp_count),
        "by_step": {step: summary(items) for step, items in by_step.items()},
        "by_kind_step": {
            f"{kind}@{step}": summary(items) for (kind, step), items in by_kind_step.items()
        },
    }
    print(
        f"{'ступень':<11} {'n':>4} {'найдено':>8} {'верно':>7} {'медиана ошибки':>15} "
        f"{'текст px':>9} {'выпрямлено':>11}"
    )
    for step, item in report["by_step"].items():
        error = item["median_error_rel"]
        print(
            f"{step:<11} {item['n']:>4} {item['found']:>8.0%} {item['correct']:>7.0%} "
            f"{(f'{error:.1%}' if error is not None else '—'):>15} {item['median_text_px']:>9.1f} "
            f"{dewarp_count[step]:>11}"
        )
    if any(item.get("frame_error") for items in by_step.values() for item in items):
        print("\nсистема координат плана по листу:")
        for step, items in by_step.items():
            truth_cases = [item for item in items if item.get("case") == "truth"]
            located = [item["frame_error"] for item in truth_cases if item["frame_error"]]
            scale = statistics.median(e["scale_rel"] for e in located) if located else None
            origin = statistics.median(e["origin_px"] for e in located) if located else None
            print(
                f"  {step:<11} найдена {len(located)}/{len(truth_cases)}"
                + (f"  масштаб ±{scale:.2%}  начало ±{origin:.1f} px" if located else "")
            )
    cases = sorted({item["case"] for items in by_step.values() for item in items if "case" in item})
    for case in cases:
        print(f"\n{case}:")
        for step, items in by_step.items():
            picked = [item for item in items if item.get("case") == case]
            if picked:
                item = summary(picked)
                print(
                    f"  {step:<11} {item['n']:>4} найдено {item['found']:>5.0%} верно {item['correct']:>5.0%}"
                )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.min_correct is not None:
        below = {
            step: item["correct"]
            for step, item in report["by_step"].items()
            if item["correct"] is not None and item["correct"] < args.min_correct
        }
        if below:
            print(f"ГЕЙТ НЕ ПРОЙДЕН (< {args.min_correct:.0%}): {below}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
