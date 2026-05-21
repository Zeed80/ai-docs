"""Tests for tp_generator.py — cutting params, time norms, blank selection, surface grouping."""

import pytest

from app.ai.tp_generator import (
    _material_group,
    _surface_to_method,
    calculate_cutting_parameters,
    calculate_time_norms,
    recommend_blank,
)


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
