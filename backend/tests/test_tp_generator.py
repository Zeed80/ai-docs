"""Tests for tp_generator.py — cutting params, time norms, blank selection, surface grouping."""

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.ai.tp_generator import (
    CompetenceCode,
    _material_group,
    _surface_to_method,
    calculate_cutting_parameters,
    calculate_time_norms,
    draft_operations_from_surfaces,
    material_group_with_confidence,
    recommend_blank,
)
from app.db.base import Base
from app.db.models import NormControlCheck


# ── _material_group ────────────────────────────────────────────────────────────

def test_material_group_steel_carbon():
    assert _material_group("Ст.45") == "steel_carbon"
    assert _material_group("сталь 20") == "steel_carbon"


def test_material_group_stainless():
    assert _material_group("12Х18Н10Т") == "stainless"
    assert _material_group("нержавейка") == "stainless"


def test_material_group_aluminum():
    assert _material_group("Алюминий АД31") == "aluminum"
    assert _material_group("Д16Т") == "aluminum"
    assert _material_group("сплав АМГ6") == "aluminum"


def test_material_group_cast_iron():
    assert _material_group("СЧ 20") == "cast_iron"


def test_material_group_default():
    assert _material_group("неизвестный материал") == "steel_carbon"


# ── material_group_with_confidence (Ф8.2 competence boundary) ──────────────────


def test_confidence_recognizes_known_steel_designations():
    group, recognized = material_group_with_confidence("Сталь 45")
    assert group == "steel_carbon"
    assert recognized is True


def test_confidence_recognizes_stainless():
    group, recognized = material_group_with_confidence("12Х18Н10Т")
    assert group == "stainless"
    assert recognized is True


def test_confidence_honestly_flags_unrecognized_material():
    """A material like titanium alloy (ВТ6) is NOT in any known group — the
    function must say so, not silently pretend it's carbon steel."""
    group, recognized = material_group_with_confidence("Титан ВТ6")
    assert group == "steel_carbon"  # still a fallback estimate...
    assert recognized is False       # ...but honestly flagged as unconfirmed


def test_confidence_flags_empty_or_nonsense_material():
    group, recognized = material_group_with_confidence("xyz-unknown-9000")
    assert recognized is False


def test_confidence_does_not_false_positive_on_titanium_grade_numbers():
    """Regression: bare "20"/"10"/"45" digit substrings previously matched
    ANY grade number, including titanium designations that happen to end in
    those digits (ВТ20, ВТ10) — the exact "titanium mistaken for steel"
    failure mode this function exists to prevent, except now with a FALSE
    recognized=True instead of an honest refusal."""
    for material in ("Титан ВТ20", "ВТ10", "Титан ВТ1-0"):
        group, recognized = material_group_with_confidence(material)
        assert recognized is False, f"{material!r} was falsely marked as a recognized steel grade"


def test_confidence_still_recognizes_real_steel_shorthand():
    for material in ("Ст3", "ст.45", "Ст 20", "СТ3сп"):
        group, recognized = material_group_with_confidence(material)
        assert group == "steel_carbon"
        assert recognized is True, f"{material!r} should still be recognized"


def test_confidence_does_not_false_positive_aluminum_on_splav_word():
    """Regression: the bare "ав" keyword (meant for the АВ aluminum grade)
    matched mid-word inside "сплав" (any alloy — Russian for "alloy"),
    falsely tagging non-aluminum materials like tool carbide as aluminum."""
    group, recognized = material_group_with_confidence("Твёрдый сплав ВК8")
    assert group != "aluminum"


def test_confidence_still_recognizes_aluminum_short_grades_as_own_token():
    for material in ("Сплав АВ", "Лист А5", "профиль А6", "лист-А7"):
        group, recognized = material_group_with_confidence(material)
        assert group == "aluminum"
        assert recognized is True, f"{material!r} should still be recognized as aluminum"


def test_cutting_parameters_carry_competence_flag_when_recognized():
    cp = calculate_cutting_parameters("turning", "Сталь 45", 30.0, 3.2)
    assert cp["competence"]["recognized"] is True
    assert cp["competence"]["code"] is None


def test_cutting_parameters_carry_competence_flag_when_unrecognized():
    cp = calculate_cutting_parameters("turning", "Титан ВТ6", 30.0, 3.2)
    assert cp["competence"]["recognized"] is False
    assert cp["competence"]["code"] == CompetenceCode.MATERIAL_UNRECOGNIZED
    assert "Титан ВТ6" in cp["competence"]["note"]
    # The estimate is still returned (draft-first) — just honestly flagged.
    assert cp["vc_m_min"] > 0


def test_cutting_parameters_logs_typed_warning_for_unrecognized_material(monkeypatch):
    from app.ai import tp_generator

    calls = []
    monkeypatch.setattr(tp_generator.logger, "warning", lambda event, **kw: calls.append((event, kw)))
    calculate_cutting_parameters("turning", "Титан ВТ6", 30.0, 3.2)
    assert calls
    event, kw = calls[0]
    assert event == "tp_cutting_params_material_unrecognized"
    assert kw["code"] == CompetenceCode.MATERIAL_UNRECOGNIZED


# ── _surface_to_method ─────────────────────────────────────────────────────────

def test_surface_to_method_hole_rough():
    assert _surface_to_method("hole", 20.0, 6.3) == "drilling"


def test_surface_to_method_hole_finish():
    # fine Ra on a hole → boring
    assert _surface_to_method("hole", 50.0, 0.8) == "boring"


def test_surface_to_method_external_cylindrical():
    assert _surface_to_method("external_cylindrical", 40.0, 3.2) == "turning"


def test_surface_to_method_flat():
    assert _surface_to_method("flat", None, None) == "milling"


def test_surface_to_method_thread():
    assert _surface_to_method("thread", 10.0, None) == "turning"


def test_surface_to_method_grinding():
    # Fine finish on external surface → grinding
    assert _surface_to_method("external_cylindrical", 30.0, 0.4) == "grinding"


# ── calculate_cutting_parameters ──────────────────────────────────────────────

def test_cutting_params_turning_steel45():
    params = calculate_cutting_parameters(
        operation_type="turning",
        material="Ст.45",
        nominal_mm=50.0,
        roughness_ra=1.6,
    )
    assert "vc_m_min" in params
    assert "n_rpm" in params
    assert "feed_mm_min" in params
    assert "ap_mm" in params
    assert "to_min" in params
    assert params["vc_m_min"] > 0
    assert params["n_rpm"] > 0
    assert params["to_min"] > 0


def test_cutting_params_milling_aluminum():
    params = calculate_cutting_parameters(
        operation_type="milling",
        material="Д16Т",
        nominal_mm=80.0,
        roughness_ra=3.2,
    )
    assert params["vc_m_min"] > 0
    # Aluminum should have higher Vc than steel
    steel_params = calculate_cutting_parameters(
        operation_type="milling",
        material="Ст.45",
        nominal_mm=80.0,
        roughness_ra=3.2,
    )
    assert params["vc_m_min"] >= steel_params["vc_m_min"]


def test_cutting_params_unknown_operation():
    # Should not raise, just return defaults
    params = calculate_cutting_parameters(
        operation_type="assembly",
        material="Ст.45",
        nominal_mm=20.0,
        roughness_ra=3.2,
    )
    assert isinstance(params, dict)
    assert params.get("to_min", 0) >= 0


# ── calculate_time_norms ──────────────────────────────────────────────────────

def test_time_norms_tsht_calculation():
    norms = calculate_time_norms(
        operation_type="turning",
        to_min=5.0,
        batch_size=10,
    )
    assert "to_minutes" in norms
    assert "tv_minutes" in norms
    assert "tsht_minutes" in norms
    assert "tsht_k_minutes" in norms
    assert "tpz_minutes" in norms

    # Tshт ≥ To (includes auxiliary time)
    assert norms["tsht_minutes"] >= norms["to_minutes"]
    # Tsht-k ≥ Tsht (includes setup amortisation)
    assert norms["tsht_k_minutes"] >= norms["tsht_minutes"]


def test_time_norms_large_batch_reduces_tsht_k():
    small = calculate_time_norms("turning", 5.0, batch_size=1)
    large = calculate_time_norms("turning", 5.0, batch_size=100)
    # Larger batch → setup cost amortised → tsht_k decreases
    assert large["tsht_k_minutes"] <= small["tsht_k_minutes"]


def test_time_norms_fractions():
    norms = calculate_time_norms("turning", to_min=10.0, batch_size=50)
    # tob + totd should be positive fractions of tsht
    assert norms.get("tob_minutes", 0) >= 0
    assert norms.get("totd_minutes", 0) >= 0


# ── recommend_blank ────────────────────────────────────────────────────────────

def test_blank_selection_high_kim_gives_rolled():
    # d=30, l=50: mass_blank ≈ 7.85e-6 * π * 15² * 50 ≈ 0.277 kg; mass_part=0.25 → KIM≈0.90
    rec = recommend_blank(
        material="Ст.45",
        dims={"d_mm": 30, "l_mm": 50},
        mass_part_kg=0.25,
        annual_volume=500,
    )
    assert rec["blank_type"] == "прокат"
    assert "utilization_factor" in rec
    assert rec["utilization_factor"] >= 0.7


def test_blank_selection_low_kim():
    rec = recommend_blank(
        material="Ст.45",
        dims={"d_mm": 200, "l_mm": 300},
        mass_part_kg=1.0,
        annual_volume=500,
    )
    # Very low КИМ (small part from large blank) → поковка/штамповка
    assert rec["blank_type"] in ("поковка", "штамповка")


def test_blank_selection_aluminum_always_rolled():
    rec = recommend_blank(
        material="АД31",
        dims={"d_mm": 30, "l_mm": 100},
        mass_part_kg=0.5,
        annual_volume=100,
    )
    assert rec["blank_type"] == "прокат"
    assert "reasoning" in rec


def test_blank_selection_has_required_keys():
    rec = recommend_blank("Ст.20", {"d_mm": 60, "l_mm": 150}, 1.0, 200)
    for key in ("blank_type", "utilization_factor", "confidence", "reasoning"):
        assert key in rec


# ── draft_operations_from_surfaces + NormControlCheck (Ф8.2 fix: competence
# must reach a real reviewer channel, not just a log line) ────────────────


def _make_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _surface_spec(**overrides) -> dict:
    spec = {
        "machining_method": "turning",
        "surface_type": "external_cylindrical",
        "nominal_mm": 30.0,
        "roughness_ra": 3.2,
        "machining_stage": "finish",
        "is_internal": False,
    }
    spec.update(overrides)
    return spec


def test_unrecognized_material_creates_a_normcontrol_check():
    engine = _make_engine()
    plan_id = uuid.uuid4()
    with Session(engine) as db:
        draft_operations_from_surfaces(
            [_surface_spec()], "Титан ВТ20", batch_size=10, plan_id=plan_id, db=db,
        )
        db.commit()

        checks = db.execute(
            select(NormControlCheck).where(NormControlCheck.check_code == CompetenceCode.MATERIAL_UNRECOGNIZED)
        ).scalars().all()
        assert len(checks) == 1
        check = checks[0]
        assert check.process_plan_id == plan_id
        assert check.operation_id is not None
        assert check.severity == "warning"
        assert check.status == "open"
        assert "Титан ВТ20" in check.message
        assert check.evidence["material_group_assumed"] == "steel_carbon"


def test_recognized_material_creates_no_normcontrol_check():
    engine = _make_engine()
    plan_id = uuid.uuid4()
    with Session(engine) as db:
        draft_operations_from_surfaces(
            [_surface_spec()], "Сталь 45", batch_size=10, plan_id=plan_id, db=db,
        )
        db.commit()

        checks = db.execute(
            select(NormControlCheck).where(NormControlCheck.check_code == CompetenceCode.MATERIAL_UNRECOGNIZED)
        ).scalars().all()
        assert checks == []


def test_normcontrol_check_operation_id_points_at_the_actual_operation():
    engine = _make_engine()
    plan_id = uuid.uuid4()
    with Session(engine) as db:
        ops = draft_operations_from_surfaces(
            [_surface_spec()], "Титан ВТ6", batch_size=10, plan_id=plan_id, db=db,
        )
        db.commit()

        turning_op = next(o for o in ops if o.operation_type == "turning")
        check = db.execute(
            select(NormControlCheck).where(NormControlCheck.check_code == CompetenceCode.MATERIAL_UNRECOGNIZED)
        ).scalar_one()
        assert check.operation_id == turning_op.id
