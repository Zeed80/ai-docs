"""F1: analytical solvers — formulas, explicit assumptions, honest verdicts."""

import math
from types import SimpleNamespace

import pytest

from app.domain.analysis_solvers import (
    SOLVERS,
    AnalysisInputError,
    solve_bending,
    solve_buckling,
    solve_thermal_expansion,
    solve_torsion,
)

STEEL = SimpleNamespace(
    yield_strength_mpa=300.0,
    elastic_modulus_mpa=200_000.0,
    thermal_expansion_1_k=1.2e-5,
)


def test_bending_cantilever_round_section():
    # F=1000 N, L=100 mm → M=1e5 N·mm; d=20 → W=π·20³/32≈785.4; σ≈127.3 MPa
    out = solve_bending({"force_n": 1000, "length_mm": 100, "diameter_mm": 20}, STEEL)
    assert out.results["stress_mpa"] == pytest.approx(1e5 / (math.pi * 20**3 / 32))
    assert out.results["safety_factor"] == pytest.approx(300 / out.results["stress_mpa"])
    assert out.passed is True
    assert any("консольная" in a for a in out.assumptions)


def test_bending_requires_section():
    with pytest.raises(AnalysisInputError):
        solve_bending({"moment_nmm": 1000}, STEEL)


def test_torsion_solid_shaft():
    # T=5e5 N·mm, d=30 → Wp=π·30³/16≈5301; τ≈94.3; limit 174 → SF≈1.85
    out = solve_torsion({"torque_nmm": 5e5, "diameter_mm": 30}, STEEL)
    wp = math.pi * 30**3 / 16
    assert out.results["shear_stress_mpa"] == pytest.approx(5e5 / wp)
    assert out.results["shear_limit_mpa"] == pytest.approx(0.58 * 300)
    assert out.passed is True


def test_buckling_euler_and_material_required():
    # d=20 → I=π·20⁴/64≈7854 mm⁴; L=1000, μ=1 → Pcr=π²·2e5·I/1e6 ≈ 15.5 kN
    out = solve_buckling({"length_mm": 1000, "force_n": 10_000, "diameter_mm": 20}, STEEL)
    inertia = math.pi * 20**4 / 64
    expected = math.pi**2 * 200_000 * inertia / 1000**2
    assert out.results["critical_force_n"] == pytest.approx(expected)
    assert out.passed is (expected / 10_000 >= 1)
    with pytest.raises(AnalysisInputError):
        solve_buckling({"length_mm": 1000, "force_n": 10, "diameter_mm": 20}, None)


def test_thermal_expansion_free_and_constrained():
    free = solve_thermal_expansion({"length_mm": 500, "delta_t_c": 80}, STEEL)
    assert free.results["elongation_mm"] == pytest.approx(1.2e-5 * 500 * 80)
    assert free.passed is None  # nothing to fail against when unconstrained
    fixed = solve_thermal_expansion(
        {"length_mm": 500, "delta_t_c": 80, "constrained": True}, STEEL
    )
    # σ = E·α·ΔT = 2e5 · 1.2e-5 · 80 = 192 MPa < 300 → passes
    assert fixed.results["thermal_stress_mpa"] == pytest.approx(192)
    assert fixed.passed is True


def test_no_material_limit_means_computed_not_passed():
    out = solve_torsion({"torque_nmm": 1e5, "diameter_mm": 20}, None)
    assert out.passed is None
    assert out.results["safety_factor"] is None


def test_registry_covers_all_documented_types():
    assert set(SOLVERS) == {
        "axial_stress", "bending", "torsion", "buckling", "thermal_expansion",
        "fea_beam",
    }


def test_fea_beam_cantilever_matches_analytics():
    """F3: FE result must reproduce the closed-form cantilever answer —
    v = FL³/3EI at the tip, σ = FL/W at the root."""
    from app.domain.analysis_solvers import solve_fea_beam

    out = solve_fea_beam(
        {"length_mm": 1000, "diameter_mm": 40,
         "supports": "cantilever",
         "loads": [{"type": "point", "force_n": 1000, "position_mm": 1000}]},
        STEEL,
    )
    inertia = math.pi * 40**4 / 64
    modulus_w = math.pi * 40**3 / 32
    v_exact = 1000 * 1000**3 / (3 * 200_000 * inertia)
    sigma_exact = 1000 * 1000 / modulus_w
    assert out.results["converged"] is True
    assert out.results["max_deflection_mm"] == pytest.approx(v_exact, rel=0.01)
    assert out.results["max_stress_mpa"] == pytest.approx(sigma_exact, rel=0.02)
    assert out.passed is True  # σ≈159 < 300


def test_fea_beam_simply_supported_udl():
    """v_max = 5qL⁴/384EI, M_max = qL²/8 for a UDL on a simple span."""
    from app.domain.analysis_solvers import solve_fea_beam

    out = solve_fea_beam(
        {"length_mm": 2000, "diameter_mm": 50,
         "supports": "simply_supported",
         "loads": [{"type": "udl", "force_n_per_mm": 2.0}]},
        STEEL,
    )
    inertia = math.pi * 50**4 / 64
    v_exact = 5 * 2.0 * 2000**4 / (384 * 200_000 * inertia)
    m_exact = 2.0 * 2000**2 / 8
    assert out.results["converged"] is True
    assert out.results["max_deflection_mm"] == pytest.approx(v_exact, rel=0.01)
    assert out.results["max_moment_nmm"] == pytest.approx(m_exact, rel=0.03)


def test_fea_beam_requires_material_and_loads():
    from app.domain.analysis_solvers import solve_fea_beam

    with pytest.raises(AnalysisInputError):
        solve_fea_beam({"length_mm": 100, "diameter_mm": 10, "loads": [{"type": "point", "force_n": 1}]}, None)
    with pytest.raises(AnalysisInputError):
        solve_fea_beam({"length_mm": 100, "diameter_mm": 10}, STEEL)
