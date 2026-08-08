from app.ai.cad_digitization_type import (
    resolve_digitization_type,
    validate_spec_for_digitization_type,
)


def test_digitization_type_maps_to_domain_profile():
    assert resolve_digitization_type("rotation_body").profile == "mechanical"
    assert resolve_digitization_type("construction_structure").profile == "construction"
    assert resolve_digitization_type("mep_systems").spec_redraw_supported is False


def test_unknown_digitization_type_fails_to_auto():
    decision = resolve_digitization_type("spaceship")
    assert decision.normalized == "auto"
    assert decision.explicit is False


def test_rotation_selection_rejects_non_rotation_spec():
    assert validate_spec_for_digitization_type(
        {"main_view": {"profile": {"shape": "rectangle"}}}, "rotation_body"
    )
    assert not validate_spec_for_digitization_type(
        {"main_view": {"outer": [{"diameter_mm": 20, "length_mm": 40}]}},
        "rotation_body",
    )
