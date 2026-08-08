#!/usr/bin/env python3
"""Parse IFC assets and publish semantic construction-corpus facts."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from typing import Any


def _relative_error(actual: float, expected: float) -> float:
    return abs(actual - expected) / max(abs(expected), 1e-12)


def _counter_prf(reference: dict[str, int], candidate: dict[str, int]) -> dict[str, float | int]:
    keys = set(reference) | set(candidate)
    matched = sum(min(reference.get(key, 0), candidate.get(key, 0)) for key in keys)
    false_positive = sum(max(0, candidate.get(key, 0) - reference.get(key, 0)) for key in keys)
    false_negative = sum(max(0, reference.get(key, 0) - candidate.get(key, 0)) for key in keys)
    precision = matched / (matched + false_positive) if matched + false_positive else 0.0
    recall = matched / (matched + false_negative) if matched + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "matched": matched,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _promotion_decision(metrics: dict[str, Any]) -> tuple[bool, list[str]]:
    checks = {
        "reference_ifc_invalid": metrics["reference_ok"],
        "candidate_ifc_invalid": metrics["candidate_ok"],
        "product_class_mismatch": metrics["product_class_f1"] >= 0.95,
        "storey_count_mismatch": metrics["storey_count_match"],
        "space_count_mismatch": metrics["space_count_match"],
        "containment_mismatch": metrics["containment_relative_error"] <= 0.05,
        "geometry_failures": metrics["candidate_geometry_failures"] == 0,
        "geometry_bbox_mismatch": metrics["bbox_max_relative_error"] <= 0.02,
        "geometry_volume_mismatch": metrics["volume_relative_error"] <= 0.02,
    }
    failures = [code for code, passed in checks.items() if not passed]
    return not failures, failures


def evaluate_ifc(path: pathlib.Path) -> dict[str, Any]:
    import ifcopenshell

    model = ifcopenshell.open(str(path))
    roots = model.by_type("IfcRoot")
    guids = [str(item.GlobalId) for item in roots if getattr(item, "GlobalId", None)]
    products = model.by_type("IfcProduct")
    represented = [item for item in products if getattr(item, "Representation", None)]
    projects = model.by_type("IfcProject")
    facts = {
        "schema": model.schema,
        "entities": sum(1 for _ in model),
        "roots": len(roots),
        "unique_guids": len(set(guids)),
        "products": len(products),
        "represented_products": len(represented),
        "sites": len(model.by_type("IfcSite")),
        "buildings": len(model.by_type("IfcBuilding")),
        "storeys": len(model.by_type("IfcBuildingStorey")),
        "spaces": len(model.by_type("IfcSpace")),
        "containment_relations": len(model.by_type("IfcRelContainedInSpatialStructure")),
        "aggregate_relations": len(model.by_type("IfcRelAggregates")),
        "distribution_elements": len(model.by_type("IfcDistributionElement")),
        "port_connections": len(model.by_type("IfcRelConnectsPorts")),
        "product_class_counts": dict(sorted(Counter(item.is_a() for item in represented).items())),
    }
    issues = []
    if not projects:
        issues.append("missing_ifc_project")
    if not roots:
        issues.append("missing_ifc_roots")
    if len(guids) != len(set(guids)):
        issues.append("duplicate_global_ids")
    return {"ok": not issues, "issues": issues, **facts}


def _geometry_facts(path: pathlib.Path) -> dict[str, Any]:
    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.shape

    model = ifcopenshell.open(str(path))
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    minimum = [float("inf")] * 3
    maximum = [float("-inf")] * 3
    volume = 0.0
    represented = 0
    failures = []
    class_volumes: Counter[str] = Counter()
    for product in model.by_type("IfcProduct"):
        if not getattr(product, "Representation", None):
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
            vertices = list(shape.geometry.verts)
            if not vertices:
                raise ValueError("empty tessellation")
            product_volume = float(ifcopenshell.util.shape.get_volume(shape.geometry))
            volume += product_volume
            class_volumes[product.is_a()] += product_volume
            represented += 1
            for index in range(0, len(vertices), 3):
                for axis in range(3):
                    value = float(vertices[index + axis])
                    minimum[axis] = min(minimum[axis], value)
                    maximum[axis] = max(maximum[axis], value)
        except Exception as exc:  # noqa: BLE001
            failures.append({
                "guid": str(getattr(product, "GlobalId", "")),
                "ifc_class": product.is_a(),
                "error": f"{type(exc).__name__}:{exc}",
            })
    bbox = [maximum[axis] - minimum[axis] for axis in range(3)] if represented else [0.0] * 3
    return {
        "represented_products": represented,
        "geometry_failures": failures,
        "bbox": bbox,
        "total_product_volume": volume,
        "class_volumes": dict(sorted(class_volumes.items())),
    }


def compare_ifc(reference_path: pathlib.Path, candidate_path: pathlib.Path) -> dict[str, Any]:
    import ifcopenshell

    reference = evaluate_ifc(reference_path)
    candidate = evaluate_ifc(candidate_path)
    reference_geometry = _geometry_facts(reference_path)
    candidate_geometry = _geometry_facts(candidate_path)
    class_metrics = _counter_prf(
        reference["product_class_counts"], candidate["product_class_counts"]
    )
    reference_bbox = sorted(reference_geometry["bbox"])
    candidate_bbox = sorted(candidate_geometry["bbox"])
    bbox_errors = [
        _relative_error(candidate_bbox[index], reference_bbox[index]) for index in range(3)
    ]
    reference_guids = {
        str(item.GlobalId)
        for item in ifcopenshell.open(str(reference_path)).by_type("IfcRoot")
        if getattr(item, "GlobalId", None)
    }
    candidate_guids = {
        str(item.GlobalId)
        for item in ifcopenshell.open(str(candidate_path)).by_type("IfcRoot")
        if getattr(item, "GlobalId", None)
    }
    metrics = {
        "reference_ok": reference["ok"],
        "candidate_ok": candidate["ok"],
        "product_class_f1": class_metrics["f1"],
        "product_class_metrics": class_metrics,
        "storey_count_match": reference["storeys"] == candidate["storeys"],
        "space_count_match": reference["spaces"] == candidate["spaces"],
        "containment_relative_error": _relative_error(
            candidate["containment_relations"], reference["containment_relations"]
        ),
        "candidate_geometry_failures": len(candidate_geometry["geometry_failures"]),
        "bbox_relative_errors_axis_invariant": bbox_errors,
        "bbox_max_relative_error": max(bbox_errors),
        "volume_relative_error": _relative_error(
            candidate_geometry["total_product_volume"],
            reference_geometry["total_product_volume"],
        ),
        "guid_overlap": len(reference_guids & candidate_guids),
        "guid_reference_count": len(reference_guids),
        "guid_candidate_count": len(candidate_guids),
    }
    promotion_eligible, failures = _promotion_decision(metrics)
    return {
        "schema_version": 1,
        "reference": str(reference_path),
        "candidate": str(candidate_path),
        "reference_facts": reference,
        "candidate_facts": candidate,
        "reference_geometry": reference_geometry,
        "candidate_geometry": candidate_geometry,
        "metrics": metrics,
        "promotion_eligible": promotion_eligible,
        "failures": failures,
        "limitations": [
            "Bounding-box dimensions are axis-invariant but aggregate volume does not prove placement.",
            "GUID overlap is diagnostic and is not a promotion gate for reconstructed IFC models.",
            "System connectivity and per-element correspondence require richer licensed holdout data.",
        ],
    }


def evaluate_manifest(manifest: pathlib.Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    records = []
    for row in rows:
        if row.get("asset_format") != "ifc":
            continue
        try:
            result = evaluate_ifc(pathlib.Path(row["output_path"]))
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "issues": [f"parse_failed:{type(exc).__name__}:{exc}"]}
        records.append({
            "source_group_id": row["source_group_id"],
            "drawing_class": row["drawing_class"],
            **result,
        })
    return {
        "ok": bool(records) and all(record["ok"] for record in records),
        "ifc_assets": len(records),
        "parsed": sum(record["ok"] for record in records),
        "failed": sum(not record["ok"] for record in records),
        "schemas": dict(sorted(Counter(record.get("schema", "failed") for record in records).items())),
        "classes": dict(sorted(Counter(record["drawing_class"] for record in records).items())),
        "totals": {
            key: sum(int(record.get(key, 0)) for record in records)
            for key in (
                "entities", "products", "represented_products", "sites", "buildings",
                "storeys", "spaces", "containment_relations", "aggregate_relations",
                "distribution_elements", "port_connections",
            )
        },
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=pathlib.Path, nargs="?")
    parser.add_argument("--reference-ifc", type=pathlib.Path)
    parser.add_argument("--candidate-ifc", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    pair_mode = args.reference_ifc is not None or args.candidate_ifc is not None
    if pair_mode:
        if args.reference_ifc is None or args.candidate_ifc is None or args.manifest is not None:
            parser.error("pair mode requires --reference-ifc and --candidate-ifc only")
        report = compare_ifc(args.reference_ifc, args.candidate_ifc)
        ok = report["promotion_eligible"]
    else:
        if args.manifest is None:
            parser.error("manifest is required outside pair mode")
        report = evaluate_manifest(args.manifest)
        ok = report["ok"]
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(rendered)
    print(rendered)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
