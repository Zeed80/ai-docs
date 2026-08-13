from copy import deepcopy

from app.ai.emg_live_stage import mechanical_live_stage_report


def _runtime() -> dict:
    return {
        "artifact_sha256": "a" * 64,
        "projection_primitive_count": 534,
        "checks": {
            "brep_valid": True,
            "manifold": True,
            "single_solid": True,
            "step_signature": True,
            "step_reopen_valid": True,
            "step_reopen_sha_matches": True,
            "artifact_deterministic": True,
            "all_views_present": True,
            "projection_nonempty": True,
        },
    }


def test_mechanical_live_result_admits_3d_and_2d_evidence():
    report = mechanical_live_stage_report("mechanical-case", _runtime())

    assert report["passed"] is True
    assert {item["stage"] for item in report["evidence"]} == {
        "model_3d_bim",
        "drawing_2d",
    }
    assert all(item["passed"] for item in report["evidence"])


def test_projection_failure_does_not_revoke_valid_3d_evidence():
    runtime = deepcopy(_runtime())
    runtime["checks"]["projection_nonempty"] = False

    report = mechanical_live_stage_report("mechanical-case", runtime)
    by_stage = {item["stage"]: item for item in report["evidence"]}

    assert report["passed"] is False
    assert by_stage["model_3d_bim"]["passed"] is True
    assert by_stage["drawing_2d"]["passed"] is False
    assert by_stage["drawing_2d"]["failed_checks"] == ["projection_nonempty"]
