#!/usr/bin/env python3
"""Parse IFC assets and publish semantic construction-corpus facts."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from typing import Any


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
    }
    issues = []
    if not projects:
        issues.append("missing_ifc_project")
    if not roots:
        issues.append("missing_ifc_roots")
    if len(guids) != len(set(guids)):
        issues.append("duplicate_global_ids")
    return {"ok": not issues, "issues": issues, **facts}


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
    parser.add_argument("manifest", type=pathlib.Path)
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    report = evaluate_manifest(args.manifest)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(rendered)
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
