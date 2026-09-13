"""Согласование на сохранённых ответах ридера против эталона (план, Ф8/E7).

Для каждого сохранённого чтения (`eval_reader_on_corpus` → jsonl с
``read_main_view`` и ``read_dimensions``): проверка по листу → согласование →
сравнение надписанных полей с эталоном ДО и ПОСЛЕ. Считается: исправлено
(было неверно → стало верно), сломано (было верно → стало неверно), принято
неверное (замер принят, но значение всё равно мимо), отдано человеку.

    python3 scripts/eval_reconcile.py --corpus ../cad-dataset-out/verify-corpus-v9-sr \\
        --readings a.jsonl b.jsonl
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_TOL = {"diameter_mm": 0.05, "length_mm": 0.05, "width_mm": 0.05}


def _truth_value(truth_view: dict, path: str, field: str) -> float | None:
    from app.ai.cad_recognize.verifiers.reconcile import _resolve

    node = _resolve({"main_view": truth_view}, path)
    value = (node or {}).get(field)
    return float(value) if isinstance(value, (int, float)) else None


def main() -> int:
    from app.ai.cad_recognize.verifiers.reconcile import apply_reconciliation, reconcile
    from app.ai.cad_recognize.verifiers.stage import verify_spec_against_sheet

    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--readings", type=pathlib.Path, nargs="+", required=True)
    parser.add_argument(
        "--reask",
        action="store_true",
        help="после согласования переспросить модель по спорным ступеням (нужен ридер)",
    )
    args = parser.parse_args()
    totals = {"refuted": 0, "adopted": 0, "fixed": 0, "broken": 0, "adopted_wrong": 0, "human": 0}
    for readings in args.readings:
        for line in readings.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            name = record["sheet"]
            view = record.get("read_main_view") or {}
            spec = {
                "main_view": view,
                "dimensions": [
                    {"value": str(text)} for text in record.get("read_dimensions") or []
                ],
            }
            png = (args.corpus / f"{name}.png").read_bytes()
            truth = json.loads((args.corpus / f"{name}.json").read_text())
            truth_view = truth["spec"]["main_view"]
            report = verify_spec_against_sheet(png, spec)
            decisions = reconcile(spec, report)
            fixed_spec, _report = apply_reconciliation(spec, report, decisions)
            if args.reask:
                import asyncio

                from app.ai.cad_recognize.verifiers.reask import reask_disputed

                fixed_spec, _report, decisions, asked = asyncio.run(
                    reask_disputed(png, fixed_spec, _report, decisions)
                )
                for entry in asked:
                    print(
                        f"   переспрос {entry['path']}.{entry['field']}: ответ {entry['answer_mm']} "
                        f"(прочитано {entry['read']:g}, замер {entry['measured']:g}) — {entry['outcome']}"
                    )
            refuted = sum(1 for item in report["items"] if item["status"] == "refuted")
            totals["refuted"] += refuted
            lines = []
            for decision in decisions:
                if decision["action"] == "ask_human":
                    totals["human"] += 1
                    lines.append(
                        f"   человеку {decision['path']}.{decision['field']}: {decision['reason']}"
                    )
                    continue
                totals["adopted"] += 1
                expected = _truth_value(truth_view, decision["path"], decision["field"])
                before = decision["read"]
                after = decision["value"]
                tol = _TOL.get(decision["field"], 0.05)
                was_right = expected is not None and abs(before - expected) <= tol
                now_right = expected is not None and abs(after - expected) <= tol
                if not was_right and now_right:
                    totals["fixed"] += 1
                    verdict = "ИСПРАВЛЕНО"
                elif was_right and not now_right:
                    totals["broken"] += 1
                    verdict = "СЛОМАНО"
                else:
                    verdict = "принято, всё равно мимо" if not now_right else "без изменения"
                    if not now_right:
                        totals["adopted_wrong"] += 1
                lines.append(
                    f"   {verdict}: {decision['path']}.{decision['field']} {before:g} → {after:g} "
                    f"(эталон {expected})"
                )
            print(f"== {name} ({readings.name}): опровергнуто {refuted}, решений {len(decisions)}")
            print("\n".join(lines) if lines else "   —")
    print("\nИТОГО:", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
