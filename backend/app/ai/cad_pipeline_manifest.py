"""Reproducible snapshot of every model and deterministic CAD component."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from app.ai.schemas import AITask
from app.ai.task_routing import get_routing_for

MANIFEST_VERSION = "1.0"
PIPELINE_REVISION = "drawing-graph-vlm-evidence-v2"

MULTI_TYPE_CANDIDATE = {
    "key": "multi-type-proposal-v2",
    "service": "cad-vectorizer",
    "endpoint": "/detect-multi-type",
    "checkpoint_step": 1059,
    "checkpoint_sha256": "166bb77a893c0a3de9a9d32d3346a40c0a090bddaf99dbb101b6d9ab07bbece8",
    "runtime_mode": "opt_in_only",
    "promotion_passed": False,
}

PROFILE_GATES: dict[str, dict[str, float]] = {
    profile: {
        "entity_precision": 0.995,
        "entity_recall": 0.995,
        "exact_sheet_rate": 0.99,
        "dxf_reopen_rate": 1.0,
        "false_exact_rate": 0.0,
    }
    for profile in ("auto", "mechanical", "construction", "electrical", "hydraulic", "pid")
}


def _route(task: AITask) -> dict[str, Any]:
    routing = get_routing_for(task)
    models = []
    try:
        from app.ai.model_registry import ModelRegistry

        registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
        for key in routing.models:
            capability = registry.models.get(key)
            models.append({
                "key": key,
                "provider": capability.provider.value if capability else None,
                "provider_model": capability.provider_model if capability else None,
            })
    except Exception:  # pragma: no cover - snapshot remains useful without catalog
        models = [{"key": key, "provider": None, "provider_model": None} for key in routing.models]
    return {
        "task": task.value,
        "models": models,
        "parameter_profile": routing.profile,
        "thinking": routing.thinking,
        "local_only": routing.local_only,
        "allow_cloud": routing.allow_cloud,
    }


def build_cad_pipeline_manifest(
    *,
    profile: str,
    method: str,
    source_sha256: str | None = None,
    input_kind: str = "source_image",
) -> dict[str, Any]:
    normalized_profile = "mechanical" if profile == "mechanical_eskd" else profile
    normalized_profile = (
        normalized_profile if normalized_profile in PROFILE_GATES else "auto"
    )
    components = {
        "preprocessor": {"kind": "deterministic", "version": "dewarp-binarize-v2"},
        "geometry": {
            "kind": "specialized_service",
            "assignment": "technical-vectorizer + CV fail-closed arbitration",
            "version": "technical-vectorizer-line-candidate",
            "authoritative": False,
            "available_candidates": [dict(MULTI_TYPE_CANDIDATE)],
        },
        "spec_reader": _route(AITask.CAD_SPEC_READ),
        "drawing_graph_reader": {
            **_route(AITask.CAD_DRAWING_GRAPH_READ),
            "contract": "engineering-drawing-graph-v1",
            "authority": "observations_only",
            "promotion_status": "experimental_fail_closed",
        },
        "drawing_graph_evidence_verifier": {
            **_route(AITask.CAD_DRAWING_GRAPH_EVIDENCE_VERIFY),
            "contract": "vlm-graph-evidence-verifier-v1",
            "scope": "source-resolution text/dimension/annotation crops",
            "classic_ocr_used": False,
            "geometry_authority": False,
            "must_differ_from_reader_model": True,
        },
        "drawing_graph_drafter": {
            "kind": "deterministic",
            "version": "drawing-graph-drafter-v1",
            "interpretation_allowed": False,
            "preserves_entity_ids": True,
            "preserves_relations": True,
        },
        "spec_drafter": {
            **_route(AITask.CAD_SPEC_DRAFT),
            "coverage": "model-dependent; fail-closed outside supported geometry",
            "deterministic_contract": "engineering-drawing-spec-v2",
            "supported_geometry": [
                "stepped_rotation_body",
                "rectangular_plate_with_through_holes",
                "circular_flange_with_through_holes",
                "equally_spaced_holes_on_bolt_circle",
                "capsule_slots",
            ],
            "reference_cases": "tools/cad-dataset/description_cases.json",
        },
        "engineering_graph": {"kind": "deterministic", "version": "engineering-graph-v1"},
        "constraint_verifier": {"kind": "deterministic", "version": "fail-closed-v1"},
        "certification": {"kind": "human", "required_signatures": ["drafter", "normcontroller"]},
    }
    reproducible = {
        "manifest_version": MANIFEST_VERSION,
        "pipeline_revision": PIPELINE_REVISION,
        "profile": normalized_profile,
        "method": method,
        "input_kind": input_kind,
        "components": components,
        "promotion_gate": PROFILE_GATES[normalized_profile],
    }
    config_sha256 = hashlib.sha256(
        json.dumps(reproducible, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return {
        **reproducible,
        "config_sha256": config_sha256,
        "source_sha256": source_sha256,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "user_extensible_via": {
            "model_assignments": "/settings/models",
            "profiles": sorted(PROFILE_GATES),
            "description_cases": "tools/cad-dataset/description_cases.json",
            "drawing_graph_cases": "tools/cad-dataset/drawing_graph_cases.json",
        },
    }
