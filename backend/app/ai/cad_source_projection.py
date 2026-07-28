"""Independent source-evidence gate for a generated CAD sheet.

This is intentionally stricter than B-Rep verification.  It asks whether the
reader supplied localized evidence for the geometry and whether the separate
raster/spec cross-check and generated-view coverage both passed.  It does not
promote the solid to ``verified`` by itself; the current revision still needs
the full source/result review gate.
"""

from __future__ import annotations

from typing import Any


def evaluate_source_projection(
    spec: dict,
    crosscheck: dict,
    solid_result: dict,
) -> dict[str, Any]:
    body = spec.get("main_view") or {}
    evidence_items: list[dict[str, Any]] = []
    for group in ("outer", "bore", "keyways", "cross_holes", "grooves", "chamfers"):
        for index, item in enumerate(body.get(group) or []):
            if not isinstance(item, dict):
                continue
            evidence_items.append({
                "path": f"main_view.{group}.{index}",
                "has_evidence": bool(item.get("evidence")),
            })
    missing_evidence = [item["path"] for item in evidence_items if not item["has_evidence"]]
    raster_checked = crosscheck.get("raster_check") == "checked"
    crosscheck_errors = [
        finding.get("message")
        for finding in crosscheck.get("findings") or []
        if finding.get("severity") == "error"
    ]
    sheet_verification = (solid_result.get("sheet") or {}).get("verification") or {}
    view_coverage = sheet_verification.get("view_coverage") or {}
    checks = {
        "localized_geometry_evidence": bool(evidence_items) and not missing_evidence,
        "raster_crosscheck_ran": raster_checked,
        "raster_crosscheck_has_no_errors": not crosscheck_errors,
        "generated_view_coverage": bool(view_coverage.get("ok")),
    }
    ok = all(checks.values())
    return {
        "ok": ok,
        "status": "passed" if ok else "insufficient_evidence",
        "checks": checks,
        "missing_evidence": missing_evidence,
        "crosscheck_errors": crosscheck_errors,
        "method": "localized_evidence+independent_raster_crosscheck+view_coverage",
    }
