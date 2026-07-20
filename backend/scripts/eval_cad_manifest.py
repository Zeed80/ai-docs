#!/usr/bin/env python3
"""Entity-level evaluation on a source-grouped raster/CadIR manifest.

This is the promotion metric for native-DXF pairs.  Pixel coverage is kept as
diagnostic evidence only and can never turn a non-exact entity result green.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def aggregate(records: list[dict]) -> dict:
    evaluated = [record for record in records if record.get("entity_metrics")]
    matched = sum(record["entity_metrics"]["micro"]["matched"] for record in evaluated)
    false_positive = sum(
        record["entity_metrics"]["micro"]["false_positive"] for record in evaluated
    )
    false_negative = sum(
        record["entity_metrics"]["micro"]["false_negative"] for record in evaluated
    )
    precision = matched / (matched + false_positive) if matched + false_positive else 1.0
    recall = matched / (matched + false_negative) if matched + false_negative else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    by_type: dict[str, dict[str, int]] = {}
    for record in evaluated:
        for kind, metrics in record["entity_metrics"]["per_type"].items():
            bucket = by_type.setdefault(
                kind, {"matched": 0, "false_positive": 0, "false_negative": 0}
            )
            for key in bucket:
                bucket[key] += metrics[key]
    per_type = {}
    for kind, values in sorted(by_type.items()):
        p = (
            values["matched"] / (values["matched"] + values["false_positive"])
            if values["matched"] + values["false_positive"]
            else 1.0
        )
        r = (
            values["matched"] / (values["matched"] + values["false_negative"])
            if values["matched"] + values["false_negative"]
            else 1.0
        )
        per_type[kind] = {
            **values,
            "precision": round(p, 6),
            "recall": round(r, 6),
            "f1": round(2 * p * r / (p + r), 6) if p + r else 0.0,
        }
    return {
        "files": len(records),
        "evaluated_files": len(evaluated),
        "declined_files": sum(record.get("declined", False) for record in records),
        "errors": sum("error" in record for record in records),
        "entity_precision": round(precision, 6),
        "entity_recall": round(recall, 6),
        "entity_f1": round(f1, 6),
        "exact_sheet_rate": round(
            sum(record.get("exact_sheet", False) for record in evaluated)
            / max(len(evaluated), 1),
            6,
        ),
        "false_exact_rate": round(
            sum(record.get("false_exact", False) for record in evaluated)
            / max(len(evaluated), 1),
            6,
        ),
        "per_type": per_type,
    }


def evaluate(
    manifest_path: pathlib.Path,
    *,
    recognizer: str,
    split: str,
    limit: int = 0,
    report_dir: pathlib.Path | None = None,
) -> dict:
    from app.ai.cad_ir.schema import CadIR
    from scripts.eval_vectorize import _recognize

    rows = [
        json.loads(line)
        for line in manifest_path.read_text().splitlines()
        if line.strip()
    ]
    rows = [row for row in rows if row.get("split") == split]
    if limit:
        rows = rows[:limit]
    records = []
    for row in rows:
        try:
            truth = CadIR.model_validate_json(pathlib.Path(row["ir"]).read_text())
            record = _recognize(
                pathlib.Path(row["image"]).read_bytes(),
                enhance=False,
                recognizer=recognizer,
                report_dir=report_dir,
                stem=row["id"],
                truth_ir=truth,
            )
            record = record or {"declined": True}
            record.update(
                {
                    "id": row["id"],
                    "source_group_id": row["source_group_id"],
                    "truth_kind": row.get("truth_kind"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - keep the benchmark exhaustive
            record = {
                "id": row["id"],
                "source_group_id": row["source_group_id"],
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
        records.append(record)
    return {
        "schema_version": 1,
        "manifest": str(manifest_path),
        "split": split,
        "recognizer": recognizer,
        "metric_contract": {
            "entity_tolerance": 0.0025,
            "promotion_precision": 0.995,
            "promotion_recall": 0.995,
            "promotion_exact_sheet_rate": 0.99,
            "pixel_coverage_can_promote": False,
        },
        "summary": aggregate(records),
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--recognizer", default="cv")
    parser.add_argument("--split", default="holdout", choices=("train", "val", "holdout"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report-dir", type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    result = evaluate(
        args.manifest,
        recognizer=args.recognizer,
        split=args.split,
        limit=args.limit,
        report_dir=args.report_dir,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0 if result["summary"]["evaluated_files"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
