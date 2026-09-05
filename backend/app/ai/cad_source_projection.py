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
    for group in (
        "outer",
        "bore",
        "keyways",
        "cross_holes",
        "axial_holes",
        "circular_hole_patterns",
        "grooves",
        "chamfers",
    ):
        for index, item in enumerate(body.get(group) or []):
            if not isinstance(item, dict):
                continue
            evidence_items.append(
                {
                    "path": f"main_view.{group}.{index}",
                    "has_evidence": bool(item.get("evidence")),
                }
            )
    missing_evidence = [item["path"] for item in evidence_items if not item["has_evidence"]]
    evidence_coverage = (
        (len(evidence_items) - len(missing_evidence)) / len(evidence_items)
        if evidence_items
        else 0.0
    )
    raster_checked = crosscheck.get("raster_check") == "checked"
    crosscheck_errors = [
        finding.get("message")
        for finding in crosscheck.get("findings") or []
        if finding.get("severity") == "error"
    ]
    sheet_verification = (solid_result.get("sheet") or {}).get("verification") or {}
    view_coverage = sheet_verification.get("view_coverage") or {}
    required_views = view_coverage.get("required") or []
    missing_views = view_coverage.get("missing") or []
    view_score = (
        (len(required_views) - len(missing_views)) / len(required_views)
        if required_views
        else (1.0 if view_coverage.get("ok") else 0.0)
    )
    raster_score = 1.0 if raster_checked and not crosscheck_errors else 0.0
    # Diagnostic only, never a promotion shortcut. Missing source evidence
    # cannot be hidden by perfect self-consistency between a solid and its own
    # generated views.
    score = round((evidence_coverage + raster_score + view_score) / 3.0, 3)
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
        "score": score,
        "score_components": {
            "localized_evidence_coverage": round(evidence_coverage, 3),
            "independent_raster_check": raster_score,
            "required_view_coverage": round(view_score, 3),
        },
        "promotion_eligible": bool(ok and score == 1.0),
        "method": "localized_evidence+independent_raster_crosscheck+view_coverage",
    }
