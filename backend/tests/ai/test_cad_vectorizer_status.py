from app.ai.cad_vectorizer_status import get_cad_vectorizer_development_status


def test_vectorizer_status_does_not_present_raster_overlap_as_accuracy() -> None:
    status = get_cad_vectorizer_development_status()

    assert status["candidate"]["promotion_status"] == "refused"
    assert status["candidate"]["production_default_changed"] is False
    assert status["candidate"]["standalone"]["raster_recall"] > 0
    assert status["candidate"]["standalone"]["entity_f1"] == 0
    assert status["candidate"]["hybrid"]["entity_f1"] < 0.1
    assert status["candidate"]["sheet_layout"]["view_f1_iou50"] == 1.0
    assert status["candidate"]["hierarchical_standalone"]["entity_f1"] == 0
    assert status["candidate"]["hierarchical_hybrid"]["entity_f1"] < 0.1
    assert status["candidate"]["evidence_heatmap"]["real_holdout_line_f1"] > 0.79
    assert status["candidate"]["evidence_heatmap"]["real_holdout_circle_f1"] == 0
    assert status["candidate"]["evidence_vectorization"]["entity_f1"] < 0.1
    assert status["candidate"]["evidence_vectorization"]["exact_sheet_rate"] == 0
    assert status["candidate"]["directional_fields"]["real_holdout_endpoint_f1"] > 0.7
    assert status["candidate"]["directional_fields"]["real_holdout_junction_f1"] > 0.6
    assert status["candidate"]["directional_vectorization"]["entity_f1"] < 0.02
    assert status["candidate"]["directional_vectorization"]["production_regression"] is True
    assert status["candidate"]["graph_iterations"]["source_snapped_entity_f1"] > 0.05
    assert status["candidate"]["graph_iterations"]["source_snapped_exact_sheet_rate"] == 0
    assert status["candidate"]["graph_iterations"]["line_only_architecture"] is True
    native = status["candidate"]["native_dxf_benchmark"]
    assert native["semantic_ground_truth"] is True
    assert native["cv_entity_f1"] < 0.1
    assert native["cv_false_exact_rate"] > 0.6
    assert native["text_f1"] == 0
    assert native["pdf_path_holdout_is_semantic_ground_truth"] is False


def test_vectorizer_status_returns_independent_copies() -> None:
    first = get_cad_vectorizer_development_status()
    first["corpus"]["projected_models"] = 0

    assert get_cad_vectorizer_development_status()["corpus"]["projected_models"] == 135
