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


_VERIFIERS = {"dimension_line": eval_dimension_line}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--verifier", choices=sorted(_VERIFIERS), required=True)
    parser.add_argument("--split", default="dev", choices=("dev", "holdout", "all"))
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in (args.corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evaluate = _VERIFIERS[args.verifier]
    by_step: dict[str, list[dict]] = defaultdict(list)
    by_kind_step: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if args.split != "all" and row.get("split") != args.split:
            continue
        png = (args.corpus / f"{row['name']}.png").read_bytes()
        truth = json.loads((args.corpus / f"{row['name']}.json").read_text(encoding="utf-8"))
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
        "by_step": {step: summary(items) for step, items in by_step.items()},
        "by_kind_step": {
            f"{kind}@{step}": summary(items) for (kind, step), items in by_kind_step.items()
        },
    }
    print(
        f"{'ступень':<11} {'n':>4} {'найдено':>8} {'верно':>7} {'медиана ошибки':>15} {'текст px':>9}"
    )
    for step, item in report["by_step"].items():
        error = item["median_error_rel"]
        print(
            f"{step:<11} {item['n']:>4} {item['found']:>8.0%} {item['correct']:>7.0%} "
            f"{(f'{error:.1%}' if error is not None else '—'):>15} {item['median_text_px']:>9.1f}"
        )
    if args.report:
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
