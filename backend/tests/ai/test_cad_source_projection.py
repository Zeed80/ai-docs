from app.ai.cad_source_projection import evaluate_source_projection


def _solid(view_ok: bool = True) -> dict:
    return {"sheet": {"verification": {"view_coverage": {"ok": view_ok}}}}


def test_source_projection_requires_localized_evidence_for_every_read_section():
    result = evaluate_source_projection(
        {"main_view": {"outer": [{"diameter_mm": 20, "length_mm": 30, "evidence": []}]}},
        {"raster_check": "checked", "findings": []},
        _solid(),
    )
    assert result["ok"] is False
    assert result["missing_evidence"] == ["main_view.outer.0"]
    assert result["score"] < 1.0
    assert result["promotion_eligible"] is False


def test_source_projection_passes_only_when_all_independent_checks_pass():
    result = evaluate_source_projection(
        {"main_view": {"outer": [{
            "diameter_mm": 20,
            "length_mm": 30,
            "evidence": [{"image_index": 0, "bbox": [1, 2, 3, 4]}],
        }]}},
        {"raster_check": "checked", "findings": []},
        _solid(),
    )
    assert result["ok"] is True
    assert result["status"] == "passed"
    assert result["score"] == 1.0
    assert result["promotion_eligible"] is True


def test_source_projection_rejects_an_independent_raster_error():
    result = evaluate_source_projection(
        {"main_view": {"outer": [{"evidence": [{"image_index": 0}]}]}},
        {"raster_check": "checked", "findings": [
            {"severity": "error", "message": "силуэт не совпадает"}
        ]},
        _solid(),
    )
    assert result["ok"] is False
    assert result["crosscheck_errors"] == ["силуэт не совпадает"]
