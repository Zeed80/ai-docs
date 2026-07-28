#!/usr/bin/env python3
"""Semantic benchmark for DrawingGraph -> CadIR -> DXF identity."""

from __future__ import annotations

import argparse
import io
import json
import pathlib
from collections import Counter

import ezdxf

from app.ai.cad_drawing_graph import (
    EngineeringDrawingGraph,
    draft_drawing_graph,
    verify_drawing_graph,
)
from app.ai.cad_ir.dxf_render import render_ir_to_dxf


def match_parameter_values(actual: list[float], expected: list[float]) -> int:
    """Count nominal values one-to-one; duplicates remain separate parameters."""
    remaining = [float(value) for value in actual]
    matched = 0
    for wanted in expected:
        tolerance = max(0.05, abs(float(wanted)) * 0.001)
        candidate = next(
            (
                index
                for index, value in enumerate(remaining)
                if abs(value - float(wanted)) <= tolerance
            ),
            None,
        )
        if candidate is not None:
            matched += 1
            remaining.pop(candidate)
    return matched


def summarize_results(results: list[dict]) -> dict:
    parameters_total = sum(result["parameters_total"] for result in results)
    parameters_matched = sum(result["parameters_matched"] for result in results)
    accepted = sum(bool(result["accepted"]) for result in results)
    false_accepts = sum(bool(result["false_accept"]) for result in results)
    passed = sum(bool(result["passed"]) for result in results)
    return {
        "cases": len(results),
        "passed": passed,
        "exact_graph_rate": passed / max(len(results), 1),
        "parameters_matched": parameters_matched,
        "parameters_total": parameters_total,
        "parameter_accuracy": parameters_matched / max(parameters_total, 1),
        "accepted_cases": accepted,
        "false_accepts": false_accepts,
        "false_accept_rate": false_accepts / max(accepted, 1),
        "dxf_reopen_rate": (
            sum(bool(result["dxf_reopens"]) for result in results)
            / max(len(results), 1)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        type=pathlib.Path,
        default="tools/cad-dataset/drawing_graph_cases.json",
    )
    parser.add_argument(
        "--out",
        type=pathlib.Path,
        default="test-results/eval_cad_drawing_graphs.json",
    )
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text())
    results = []
    for case in cases:
        errors: list[str] = []
        graph = EngineeringDrawingGraph.model_validate(case["graph"])
        verification = verify_drawing_graph(
            graph, pixel_recall=1.0, pixel_precision=1.0
        )
        ir = draft_drawing_graph(graph, verification=verification)
        expected = case["expected"]
        counts = dict(Counter(entity.type for entity in ir.entities))
        ids = [entity.id for entity in ir.entities]
        relation_kinds = sorted(relation.kind for relation in ir.relations)
        texts = sorted(
            entity.text for entity in ir.entities if entity.type == "text"
        )
        dimensions = sorted(
            entity.value_mm
            for entity in ir.entities
            if entity.type == "dimension" and entity.value_mm is not None
        )
        expected_values = sorted(
            float(value) for value in expected["dimension_values_mm"]
        )
        parameters_matched = match_parameter_values(dimensions, expected_values)
        if counts != expected["entity_counts"]:
            errors.append(f"counts={counts} expected={expected['entity_counts']}")
        if ids != expected["entity_ids"]:
            errors.append(f"ids={ids} expected={expected['entity_ids']}")
        if relation_kinds != sorted(expected["relation_kinds"]):
            errors.append(
                f"relations={relation_kinds} expected={expected['relation_kinds']}"
            )
        if texts != sorted(expected["texts"]):
            errors.append(f"texts={texts} expected={expected['texts']}")
        if dimensions != sorted(expected["dimension_values_mm"]):
            errors.append(
                f"dimensions={dimensions} expected={expected['dimension_values_mm']}"
            )
        try:
            doc = ezdxf.read(io.StringIO(render_ir_to_dxf(ir).decode()))
            dxf_types = sorted({entity.dxftype() for entity in doc.modelspace()})
            missing_dxf = sorted(set(expected["dxf_types"]) - set(dxf_types))
            if missing_dxf:
                errors.append(f"dxf_missing={missing_dxf}")
            dxf_reopens = True
        except Exception as exc:  # noqa: BLE001
            dxf_types = []
            dxf_reopens = False
            errors.append(f"dxf={str(exc)[:120]}")
        if ir.digitization_status != "exact_candidate":
            errors.append(f"status={ir.digitization_status}")
        if not verification.exact_ready:
            errors.append(
                "verification=" + ",".join(
                    issue.code for issue in verification.blocking
                )
            )
        accepted = bool(
            ir.digitization_status == "exact_candidate" and verification.exact_ready
        )
        false_accept = bool(accepted and errors)
        results.append({
            "id": case["id"],
            "passed": not errors,
            "errors": errors,
            "entity_counts": counts,
            "entity_ids_preserved": ids == expected["entity_ids"],
            "relation_kinds": relation_kinds,
            "texts": texts,
            "dimension_values_mm": dimensions,
            "parameters_matched": parameters_matched,
            "parameters_total": len(expected_values),
            "accepted": accepted,
            "false_accept": false_accept,
            "dxf_types": dxf_types,
            "dxf_reopens": dxf_reopens,
        })

    summary = summarize_results(results)
    report = {
        "contract": "engineering-drawing-graph-cadir-dxf-v2",
        **summary,
        "promotion_contract": {
            "parameter_accuracy": 1.0,
            "false_accept_rate": 0.0,
            "dxf_reopen_rate": 1.0,
        },
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    promotion_passed = bool(
        summary["exact_graph_rate"] == 1.0
        and summary["parameter_accuracy"] == 1.0
        and summary["false_accept_rate"] == 0.0
        and summary["dxf_reopen_rate"] == 1.0
    )
    return 0 if promotion_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
