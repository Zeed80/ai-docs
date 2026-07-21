from app.ai.cad_profile import choose_profile


def test_user_selected_profile_is_authoritative() -> None:
    decision = choose_profile("mechanical_eskd", ["ПЛАН 1:100"])
    assert decision.profile == "mechanical"
    assert decision.confidence == 1.0


def test_construction_profile_requires_multiple_signals() -> None:
    decision = choose_profile(
        "auto",
        ["План первого этажа", "Оси здания", "Экспликация помещений"],
    )
    assert decision.profile == "construction"
    assert decision.confidence > 0.5


def test_ambiguous_auto_profile_stays_unknown() -> None:
    decision = choose_profile("auto", ["Чертеж общего вида"])
    assert decision.profile == "auto"
    assert decision.confidence == 0.0


def test_explicit_construction_filename_is_sufficient_evidence() -> None:
    decision = choose_profile("auto", [], "Фасад 1-3.dwg")
    assert decision.profile == "construction"


def test_explicit_extended_profiles_are_preserved() -> None:
    for profile in ("electrical", "hydraulic", "pid"):
        decision = choose_profile(profile, ["ambiguous title"])
        assert decision.profile == profile
        assert decision.confidence == 1.0
        assert decision.evidence == ("user_selected",)
