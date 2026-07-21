#!/usr/bin/env python3
"""Exact deterministic benchmark for description -> spec -> CadIR -> DXF."""

from __future__ import annotations

import argparse
import io
import json
import pathlib
from collections import Counter

import ezdxf

from app.ai.cad_ir.dxf_render import render_ir_to_dxf
from app.ai.cad_recognize.spec_vectorize import (
    EngineeringDrawingSpec,
    draft_from_spec,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases", default="tools/cad-dataset/description_cases.json", type=pathlib.Path
    )
    parser.add_argument(
        "--out", default="test-results/eval_cad_descriptions.json", type=pathlib.Path
    )
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text())
    results = []
    for case in cases:
        validated = EngineeringDrawingSpec.model_validate(case["spec"])
        spec = validated.model_dump(mode="json")
        errors = []
        if spec["unresolved"]:
            errors.append(f"unresolved={spec['unresolved']}")
        ir = draft_from_spec(spec, sheet_format="A3", landscape=True)
        if ir is None:
            errors.append("drafter_declined")
            counts = Counter()
            values = []
            dxf_reopens = False
        else:
            counts = Counter(entity.type for entity in ir.entities)
            values = sorted(
                round(float(entity.value_mm), 6)
                for entity in ir.entities
                if entity.type == "dimension" and entity.value_mm is not None
            )
            try:
                ezdxf.read(io.StringIO(render_ir_to_dxf(ir).decode("utf-8")))
                dxf_reopens = True
            except Exception as exc:  # noqa: BLE001
                dxf_reopens = False
                errors.append(f"dxf={str(exc)[:120]}")
        expected = case["expected"]
        expected_counts = expected["entity_counts"]
        if dict(counts) != expected_counts:
            errors.append(f"counts={dict(counts)} expected={expected_counts}")
        expected_values = sorted(float(value) for value in expected["dimension_values_mm"])
        if values != expected_values:
            errors.append(f"dimensions={values} expected={expected_values}")
        if not dxf_reopens:
            errors.append("dxf_reopens=false")
        results.append({
            "id": case["id"],
            "profile": case["profile"],
            "passed": not errors,
            "errors": errors,
            "entity_counts": dict(counts),
            "dimension_values_mm": values,
            "dxf_reopens": dxf_reopens,
        })

    passed = sum(result["passed"] for result in results)
    report = {
        "contract": "description-spec-cadir-dxf-v1",
        "cases": len(results),
        "passed": passed,
        "exact_case_rate": passed / max(len(results), 1),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
