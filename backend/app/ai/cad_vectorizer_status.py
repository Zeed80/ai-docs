"""Auditable status of the current scan-to-CAD development candidate.

This is deliberately not called an accuracy score.  The values below come
from the source-grouped web STEP experiment and the independent local holdout
gate.  Updating the candidate requires updating this record together with the
evaluation report and plan.
"""

from __future__ import annotations

from typing import Any


CAD_VECTORIZER_DEVELOPMENT_STATUS: dict[str, Any] = {
        "pipeline_revision": "drawing-graph-staged-reader-v3",
    "evaluated_at": "2026-07-21",
    "accuracy_contract": {
        "mode": "fail_closed_two_person_certification",
        "vlm_is_authoritative": False,
        "normalized_db_projection_required": True,
        "required_signatures": ["drafter", "normcontroller"],
    },
    "selected_model_direction": {
        "kind": "hierarchical_multi_type_cad_vectorizer",
        "deployment": "experimental",
        "hardware_budget": "single_rtx_3090_24gb",
        "stages": [
            "sheet_layout",
            "source_resolution_tiles",
            "primitive_and_symbol_heads",
            "engineering_graph",
            "constraint_solver",
        ],
        "heads": [
            "line", "endpoint", "junction", "circle", "arc",
            "text", "dimension", "annotation", "hatch",
        ],
        "rejected_as_authoritative": [
            "qwen3_vl", "zero_to_cad_qwen3_vl", "line_only_vectorizer",
        ],
        "promotion_requires_profile_gate": True,
    },
    "corpus": {
        "licensed_web_assets": 216,
        "step_assets": 181,
        "projected_models": 135,
        "exact_sheets": 402,
        "exact_entities": 46_654,
        "training_tiles": 1_320,
        "train_tiles": 1_030,
        "validation_tiles": 192,
        "layout_sheets": 1_008,
        "layout_view_targets": 3_021,
        "native_dxf_source_groups": 29,
        "native_dxf_pairs": 106,
        "native_dxf_holdout_pairs": 6,
        "native_dxf_entities": 1_661,
    },
    "latest_real_stack_regression": {
        "evaluated_at": "2026-07-21",
        "dwg_files": 10,
        "photo_files": 19,
        "entity_precision": 0.055452,
        "entity_recall": 0.035688,
        "entity_f1": 0.043427,
        "exact_sheet_rate": 0.0,
        "false_exact_rate": 1.0,
        "dxf_reopen_rate": 1.0,
        "promotion_passed": False,
        "entity_f1_by_type": {
            "segment": 0.033300,
            "circle": 0.669903,
            "arc": 0.0,
            "hatch": 0.0,
            "text": 0.0,
        },
    },
    "description_drafting": {
        "contract": "description-spec-cadir-dxf-v2",
        "reference_cases": "tools/cad-dataset/description_cases.json",
        "evaluated_cases": 5,
        "passed_cases": 5,
        "exact_case_rate": 1.0,
        "dxf_reopen_rate": 1.0,
        "direct_text_without_image": True,
        "unresolved_is_blocking": True,
        "supported_geometry": [
            "stepped_rotation_body",
            "rectangular_plate_with_through_holes",
            "circular_flange_with_through_holes",
            "equally_spaced_holes_on_bolt_circle",
            "capsule_slots",
        ],
        "scope_warning": "Exact only for explicit values inside supported deterministic profiles",
    },
    "drawing_graph_drafting": {
        "contract": "engineering-drawing-graph-cadir-dxf-v1",
        "schema_version": 1,
        "reference_cases": "tools/cad-dataset/drawing_graph_cases.json",
        "evaluated_cases": 2,
        "passed_cases": 2,
        "exact_graph_rate": 1.0,
        "dxf_reopen_rate": 1.0,
        "entity_ids_preserved": True,
        "relations_preserved": True,
        "graph_first_enabled": True,
        "staged_reader_enabled": True,
        "fragment_tile_size_px": 1000,
        "fragment_overlap_px": 120,
        "fragment_max_tiles": 16,
        "vlm_crop_evidence_required": True,
        "classic_ocr_used": False,
        "reader_promotion_passed": False,
        "scope_warning": (
            "Contract/drafter benchmark only; the experimental reader has not "
            "passed a real full-sheet recognition gate"
        ),
    },
    "candidate": {
        "checkpoint_step": 1_000,
        "standalone": {
            "raster_precision": 0.2295,
            "raster_recall": 0.2635,
            "entity_precision": 0.0,
            "entity_recall": 0.0,
            "entity_f1": 0.0,
            "exact_sheet_rate": 0.0,
        },
        "hybrid": {
            "entity_precision": 0.193738,
            "entity_recall": 0.038253,
            "entity_f1": 0.063892,
            "exact_sheet_rate": 0.0,
            "dxf_reopen_rate": 1.0,
        },
        "sheet_layout": {
            "reserved_web_holdout_sheets": 39,
            "view_precision_iou50": 1.0,
            "view_recall_iou50": 1.0,
            "view_f1_iou50": 1.0,
            "mean_matched_iou": 0.928079,
            "exact_layout_rate": 1.0,
        },
        "hierarchical_standalone": {
            "raster_precision": 0.2207,
            "raster_recall": 0.1295,
            "entity_precision": 0.0,
            "entity_recall": 0.0,
            "entity_f1": 0.0,
            "exact_sheet_rate": 0.0,
        },
        "hierarchical_hybrid": {
            "entity_precision": 0.193738,
            "entity_recall": 0.038253,
            "entity_f1": 0.063892,
            "exact_sheet_rate": 0.0,
            "dxf_reopen_rate": 1.0,
        },
        "evidence_heatmap": {
            "checkpoint_step": 1_200,
            "validation_macro_f1": 0.210539,
            "real_holdout_line_precision": 0.723886,
            "real_holdout_line_recall": 0.875653,
            "real_holdout_line_f1": 0.792570,
            "real_holdout_circle_f1": 0.0,
            "real_holdout_arc_f1": 0.0,
            "real_holdout_macro_f1": 0.264190,
        },
        "evidence_vectorization": {
            "entity_precision": 0.215812,
            "entity_recall": 0.039026,
            "entity_f1": 0.066099,
            "exact_sheet_rate": 0.0,
            "dxf_reopen_rate": 1.0,
            "false_exact_rate": 0.0,
            "source_coordinates_preserved": True,
        },
        "directional_fields": {
            "checkpoint_step": 1_800,
            "validation_selection_score": 0.375319,
            "real_holdout_line_f1": 0.798937,
            "real_holdout_endpoint_f1": 0.714469,
            "real_holdout_junction_f1": 0.643966,
            "real_holdout_direction_cosine": 0.780814,
            "real_holdout_circle_f1": 0.0,
            "real_holdout_arc_f1": 0.0,
        },
        "directional_vectorization": {
            "entity_precision": 0.028436,
            "entity_recall": 0.013910,
            "entity_f1": 0.018682,
            "exact_sheet_rate": 0.0,
            "dxf_reopen_rate": 1.0,
            "false_exact_rate": 0.0,
            "decoder_selection_split": "source_grouped_val",
            "production_regression": True,
        },
        "graph_iterations": {
            "unordered_query_graph_best_validation_f1": 0.000062,
            "dense_edge_verifier_validation_f1": 0.006717,
            "dense_edge_verifier_full_sheet_holdout_f1": 0.001981,
            "tiled_edge_graph_entity_precision": 0.030769,
            "tiled_edge_graph_entity_recall": 0.027048,
            "tiled_edge_graph_entity_f1": 0.028789,
            "source_snapped_entity_precision": 0.084000,
            "source_snapped_entity_recall": 0.040572,
            "source_snapped_entity_f1": 0.054716,
            "source_snapped_exact_sheet_rate": 0.0,
            "source_snapped_dxf_reopen_rate": 1.0,
            "line_only_architecture": True,
        },
        "native_dxf_benchmark": {
            "truth_kind": "native_dxf_entities",
            "semantic_ground_truth": True,
            "cv_entity_precision": 0.190476,
            "cv_entity_recall": 0.045714,
            "cv_entity_f1": 0.073733,
            "cv_exact_sheet_rate": 0.0,
            "cv_false_exact_rate": 0.666667,
            "circle_f1": 0.666667,
            "arc_f1": 0.0,
            "segment_f1": 0.088889,
            "text_f1": 0.0,
            "pdf_path_holdout_is_semantic_ground_truth": False,
        },
        "multi_type_proposal": {
            "architecture": "multi-type-proposal-v2",
            "checkpoint_step": 1059,
            "checkpoint_sha256": "166bb77a893c0a3de9a9d32d3346a40c0a090bddaf99dbb101b6d9ab07bbece8",
            "training_source": "synthetic_profile_then_real_native_dxf",
            "independent_holdout_sheets": 6,
            "proposal_tolerance": 0.01,
            "entity_precision": 0.025180,
            "entity_recall": 0.019663,
            "entity_f1": 0.022082,
            "segment_f1": 0.004556,
            "circle_f1": 0.0,
            "arc_f1": 0.0,
            "text_anchor_f1": 0.067416,
            "dimension_f1": 0.0,
            "annotation_f1": 0.0,
            "hatch_f1": 0.0,
            "ocr_payload_included": False,
            "runtime_mode": "opt_in_only",
            "promotion_passed": False,
        },
        "promotion_status": "refused",
        "promotion_thresholds": {
            "entity_precision": 0.995,
            "entity_recall": 0.995,
            "exact_sheet_rate": 0.99,
            "dxf_reopen_rate": 1.0,
            "false_exact_rate": 0.0,
        },
        "production_default_changed": False,
    },
}


def get_cad_vectorizer_development_status() -> dict[str, Any]:
    """Return a copy so an API caller cannot mutate process-wide evidence."""

    from app.ai.cad_pipeline_manifest import build_cad_pipeline_manifest

    corpus = dict(CAD_VECTORIZER_DEVELOPMENT_STATUS["corpus"])
    candidate = CAD_VECTORIZER_DEVELOPMENT_STATUS["candidate"]
    latest = CAD_VECTORIZER_DEVELOPMENT_STATUS["latest_real_stack_regression"]
    description = CAD_VECTORIZER_DEVELOPMENT_STATUS["description_drafting"]
    return {
        **CAD_VECTORIZER_DEVELOPMENT_STATUS,
        "runtime_pipeline": build_cad_pipeline_manifest(
            profile="auto", method="trace"
        ),
        "accuracy_contract": {
            **CAD_VECTORIZER_DEVELOPMENT_STATUS["accuracy_contract"],
            "required_signatures": list(CAD_VECTORIZER_DEVELOPMENT_STATUS["accuracy_contract"]["required_signatures"]),
        },
        "selected_model_direction": {
            **CAD_VECTORIZER_DEVELOPMENT_STATUS["selected_model_direction"],
            "stages": list(CAD_VECTORIZER_DEVELOPMENT_STATUS["selected_model_direction"]["stages"]),
            "heads": list(CAD_VECTORIZER_DEVELOPMENT_STATUS["selected_model_direction"]["heads"]),
            "rejected_as_authoritative": list(CAD_VECTORIZER_DEVELOPMENT_STATUS["selected_model_direction"]["rejected_as_authoritative"]),
        },
        "corpus": corpus,
        "latest_real_stack_regression": {
            **latest,
            "entity_f1_by_type": dict(latest["entity_f1_by_type"]),
        },
        "description_drafting": {
            **description,
            "supported_geometry": list(description["supported_geometry"]),
        },
        "candidate": {
            **candidate,
            "standalone": dict(candidate["standalone"]),
            "hybrid": dict(candidate["hybrid"]),
            "sheet_layout": dict(candidate["sheet_layout"]),
            "hierarchical_standalone": dict(candidate["hierarchical_standalone"]),
            "hierarchical_hybrid": dict(candidate["hierarchical_hybrid"]),
            "evidence_heatmap": dict(candidate["evidence_heatmap"]),
            "evidence_vectorization": dict(candidate["evidence_vectorization"]),
            "directional_fields": dict(candidate["directional_fields"]),
            "directional_vectorization": dict(candidate["directional_vectorization"]),
            "graph_iterations": dict(candidate["graph_iterations"]),
            "native_dxf_benchmark": dict(candidate["native_dxf_benchmark"]),
            "multi_type_proposal": dict(candidate["multi_type_proposal"]),
            "promotion_thresholds": dict(candidate["promotion_thresholds"]),
        },
    }
