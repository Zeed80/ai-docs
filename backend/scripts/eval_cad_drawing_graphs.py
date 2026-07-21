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
        results.append({
            "id": case["id"],
            "passed": not errors,
            "errors": errors,
            "entity_counts": counts,
            "entity_ids_preserved": ids == expected["entity_ids"],
            "relation_kinds": relation_kinds,
            "texts": texts,
            "dimension_values_mm": dimensions,
            "dxf_types": dxf_types,
            "dxf_reopens": dxf_reopens,
        })

    passed = sum(result["passed"] for result in results)
    report = {
        "contract": "engineering-drawing-graph-cadir-dxf-v1",
        "cases": len(results),
        "passed": passed,
        "exact_graph_rate": passed / max(len(results), 1),
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
