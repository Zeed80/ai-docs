from app.ai.cad_pipeline_manifest import build_cad_pipeline_manifest


def test_pipeline_manifest_is_reproducible_and_exposes_assignments():
    first = build_cad_pipeline_manifest(
        profile="mechanical", method="trace", source_sha256="a" * 64
    )
    second = build_cad_pipeline_manifest(
        profile="mechanical", method="trace", source_sha256="a" * 64
    )

    assert first["config_sha256"] == second["config_sha256"]
    assert first["captured_at"] != ""
    assert first["components"]["spec_reader"]["task"] == "cad_spec_read"
    assert first["components"]["spec_drafter"]["task"] == "cad_spec_draft"
    assert first["promotion_gate"]["false_exact_rate"] == 0.0
    candidate = first["components"]["geometry"]["available_candidates"][0]
    assert candidate["endpoint"] == "/detect-multi-type"
    assert candidate["runtime_mode"] == "opt_in_only"
    assert candidate["promotion_passed"] is False


def test_unknown_profile_fails_to_auto_without_weakening_gate():
    manifest = build_cad_pipeline_manifest(profile="unknown", method="spec")

    assert manifest["profile"] == "auto"
    assert manifest["promotion_gate"]["entity_precision"] == 0.995


def test_eskd_ui_alias_maps_to_mechanical_profile():
    manifest = build_cad_pipeline_manifest(
        profile="mechanical_eskd",
        method="spec",
    )

    assert manifest["profile"] == "mechanical"
