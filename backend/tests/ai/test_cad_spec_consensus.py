"""Consensus reading: keep what the passes agree on, flag what they don't."""

from __future__ import annotations

from app.ai.cad_recognize.spec_consensus import consensus_spec


def _read(outer, **extra) -> dict:
    spec = {
        "part": "Вал",
        "main_view": {"type": "тело вращения (вал)", "outer": outer},
        "title_block": {"material": "Сталь 45"},
    }
    spec["main_view"].update(extra.pop("main_view", {}))
    spec.update(extra)
    return spec


_PROFILE = [
    {"diameter_mm": 30, "length_mm": 40},
    {"diameter_mm": 50, "length_mm": 60},
]


def test_agreeing_passes_keep_the_profile():
    spec = consensus_spec([_read(_PROFILE), _read(_PROFILE), _read(_PROFILE)])
    assert len(spec["main_view"]["outer"]) == 2
    assert spec["unresolved"] == []
    assert spec["consensus"]["usable"] == 3


def test_small_reading_noise_still_counts_as_agreement():
    """559.9 and 560.0 off the same sheet are the same dimension."""
    noisy = [
        {"diameter_mm": 30.1, "length_mm": 40},
        {"diameter_mm": 50, "length_mm": 60},
    ]
    spec = consensus_spec([_read(_PROFILE), _read(noisy), _read(_PROFILE)])
    assert spec["main_view"]["outer"]
    assert spec["unresolved"] == []


def test_a_profile_that_changes_between_passes_is_refused():
    """The exact live failure: the same sheet read differently twice."""
    other = [
        {"diameter_mm": 30, "length_mm": 40},
        {"diameter_mm": 80, "length_mm": 60},
    ]
    third = [{"diameter_mm": 30, "length_mm": 40}]
    spec = consensus_spec([_read(_PROFILE), _read(other), _read(third)])
    assert "outer" not in spec["main_view"]
    assert any("не сошлись на профиле" in item for item in spec["unresolved"])


def test_consensus_never_invents_a_profile_no_pass_described():
    """No chimera of one pass's diameters and another's lengths."""
    a = [{"diameter_mm": 30, "length_mm": 40}, {"diameter_mm": 50, "length_mm": 60}]
    b = [{"diameter_mm": 30, "length_mm": 99}, {"diameter_mm": 50, "length_mm": 60}]
    spec = consensus_spec([_read(a), _read(b)])
    assert "outer" not in spec["main_view"]


def test_a_value_only_one_pass_saw_is_not_confirmed():
    reads = [
        _read(_PROFILE, dimensions=[{"value": "Ø50js6"}, {"value": "Ø999"}]),
        _read(_PROFILE, dimensions=[{"value": "Ø50js6"}]),
        _read(_PROFILE, dimensions=[{"value": "Ø50js6"}]),
    ]
    spec = consensus_spec(reads)
    values = [d["value"] for d in spec["dimensions"]]
    assert values == ["Ø50js6"]


def test_a_disputed_stamp_field_is_optional_not_blocking():
    reads = [
        _read(_PROFILE, title_block={"material": "Сталь 45"}),
        _read(_PROFILE, title_block={"material": "Чугун СЧ20"}),
        _read(_PROFILE, title_block={"material": "Сталь 20"}),
    ]
    spec = consensus_spec(reads)
    assert "material" not in spec["title_block"]
    assert any("material" in item for item in spec["optional_unresolved"])
    assert spec["unresolved"] == []


def test_one_pass_admitting_it_could_not_prove_a_value_is_enough_to_block():
    reads = [
        _read(_PROFILE),
        _read(_PROFILE, unresolved=["габарит не найден"]),
        _read(_PROFILE),
    ]
    spec = consensus_spec(reads)
    assert "габарит не найден" in spec["unresolved"]


def test_a_bore_only_some_passes_saw_becomes_a_review_item():
    reads = [
        _read(_PROFILE, main_view={"bore": [{"diameter_mm": 16, "length_mm": 90}]}),
        _read(_PROFILE),
        _read(_PROFILE),
    ]
    spec = consensus_spec(reads)
    assert "bore" not in spec["main_view"]
    assert any("расточка" in item for item in spec["unresolved"])


def test_a_flange_profile_needs_more_than_one_pass_to_survive():
    """Live: one pass returned a complete circle profile, another returned none."""
    flange = {"shape": "circle", "diameter_mm": 560, "thickness_mm": 20, "holes": []}
    seen = _read([], main_view={"profile": flange})
    unseen = _read([])
    spec = consensus_spec([seen, unseen, unseen])
    assert "profile" not in spec["main_view"]
    assert any("контур" in item for item in spec["unresolved"])

    twice = consensus_spec([seen, seen, unseen])
    assert twice["main_view"]["profile"]["diameter_mm"] == 560


def test_a_single_usable_pass_is_passed_through_unchanged():
    spec = consensus_spec([_read(_PROFILE)])
    assert spec["main_view"]["outer"] == _PROFILE
    assert spec["consensus"]["agreement"] == "single_pass"


def test_no_usable_reads_yield_nothing():
    assert consensus_spec([]) == {}
    assert consensus_spec([{}, {}]) == {}
