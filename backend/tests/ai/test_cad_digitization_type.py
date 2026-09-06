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


# ── Гейт обязан указывать на настоящую причину ───────────────────────────────


def test_no_geometry_blames_the_read_not_the_chosen_type():
    """Живой прогон z4-r4.jpg: модель ТРИЖДЫ правильно прочитала вал.

    Профиль потерялся уже после чтения — на слиянии, — а оператор получил
    «выбран тип „тело вращения“, но чтение не подтвердило осевой ступенчатый
    профиль», то есть обвинение в неверно выбранном типе детали. Проверка здесь
    смотрит ровно на пустоту ``main_view.outer`` и про тип не знает ничего,
    поэтому и говорить про тип она не должна.
    """
    spec = {
        "main_view": {"type": "unknown"},
        "observation_only": True,
        "geometry_validation_errors": ["main_view.grooves.0: groove needs exactly one of ..."],
    }
    blockers = validate_spec_for_digitization_type(spec, "rotation_body")

    assert len(blockers) == 1
    message = blockers[0]
    assert "Чтение не дало геометрии" in message
    # Настоящая причина названа, а не спрятана в логе воркера.
    assert "groove needs exactly one" in message


def test_non_axial_geometry_still_reports_a_type_mismatch():
    """Обратный случай: геометрия есть, но она не осевая — тут тип и правда ни при чём не годится."""
    spec = {"main_view": {"type": "призматическая", "profile": {"shape": "rectangle"}}}
    blockers = validate_spec_for_digitization_type(spec, "rotation_body")

    assert len(blockers) == 1
    assert "не подтвердило осевой ступенчатый профиль" in blockers[0]


def test_a_read_shaft_passes_the_gate():
    spec = {"main_view": {"type": "тело вращения (вал)", "outer": [{"diameter_mm": 30}]}}
    assert validate_spec_for_digitization_type(spec, "rotation_body") == []
