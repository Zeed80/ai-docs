"""Уровень 10 графа: STEP, переоткрытый ядром, — свидетельство сборки."""

from __future__ import annotations

from app.ai.cad_emg_compat import kernel_reopen_patch, spec_feature_tree_as_graph
from app.ai.cad_recognize.spec_vectorize import EngineeringDrawingSpec
from app.ai.cad_solid import feature_tree_from_spec
from app.domain.engineering_model_graph import apply_graph_patch
from app.services.engineering_model_graph import verify_graph

SPEC = {
    "part_class": "rotation",
    "main_view": {
        "type": "тело вращения",
        "outer": [
            {"diameter_mm": 30.0, "length_mm": 40.0},
            {"diameter_mm": 20.0, "length_mm": 30.0},
        ],
    },
}


def _graph():
    spec = EngineeringDrawingSpec.model_validate(SPEC).model_dump(mode="json")
    return spec_feature_tree_as_graph(spec, feature_tree_from_spec(spec), graph_id="g")


def _report(reopened_volume: float, valid: bool = True) -> dict:
    return {
        "volume_mm3": 37699.1,
        "reopen": {
            "valid": valid,
            "brep_valid": valid,
            "solid_count": 1,
            "face_count": 5,
            "volume_mm3": reopened_volume,
            "step_sha256": "a" * 64,
        },
    }


def _codes(graph) -> list[str]:
    return [issue["code"] for issue in verify_graph(graph)[1]]


def test_a_step_reopened_by_the_kernel_is_level_10_evidence():
    """У каждой механической сборки стояло brep_ifc_reopen_not_available, хотя ядро
    переоткрывало STEP, — результат терялся до графа."""
    graph = _graph()
    assert "brep_ifc_reopen_not_available" in _codes(graph)

    patch = kernel_reopen_patch(graph, _report(37699.3), pass_id="kernel-1")

    assert patch is not None
    assert "brep_ifc_reopen_not_available" not in _codes(apply_graph_patch(graph, patch))


def test_an_invalid_or_different_reopened_step_is_no_evidence():
    graph = _graph()
    assert kernel_reopen_patch(graph, _report(37699.3, valid=False), pass_id="k") is None
    assert kernel_reopen_patch(graph, _report(30000.0), pass_id="k") is None
    assert kernel_reopen_patch(graph, {"volume_mm3": 1.0}, pass_id="k") is None
