"""Translate exact live runtime checks into class-balanced stage evidence."""

from __future__ import annotations

from typing import Any


_MECHANICAL_3D_CHECKS = {
    "brep_valid",
    "manifold",
    "single_solid",
    "step_signature",
    "step_reopen_valid",
    "step_reopen_sha_matches",
    "artifact_deterministic",
}
_MECHANICAL_2D_CHECKS = _MECHANICAL_3D_CHECKS | {
    "all_views_present",
    "projection_nonempty",
}


def mechanical_live_stage_report(
    case_id: str, runtime_result: dict[str, Any]
) -> dict[str, Any]:
    checks = runtime_result.get("checks") or {}

    def evidence(stage: str, required: set[str]) -> dict[str, Any]:
        missing = sorted(key for key in required if checks.get(key) is not True)
        return {
            "id": f"live:{case_id}:{stage}",
            "case_id": case_id,
            "stage": stage,
            "passed": not missing,
            "required_checks": sorted(required),
            "failed_checks": missing,
            "artifact_sha256": runtime_result.get("artifact_sha256"),
            "projection_primitive_count": runtime_result.get(
                "projection_primitive_count"
            ),
        }

    entries = [
        evidence("model_3d_bim", _MECHANICAL_3D_CHECKS),
        evidence("drawing_2d", _MECHANICAL_2D_CHECKS),
    ]
    return {
        "schema_version": "emg-live-stage-report/1.0",
        "split": "dev",
        "case_id": case_id,
        "passed": all(item["passed"] for item in entries),
        "evidence": entries,
    }
