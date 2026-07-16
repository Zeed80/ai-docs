"""F1: deterministic analytical solvers for engineering analysis cases.

Every solver is a pure function over explicit, unit-suffixed inputs plus an
optional material card. It returns the computed results, the ASSUMPTIONS the
formula rests on (spelled out, not implied — сопромат formulas are only valid
inside their model), and a pass/fail verdict against the material limit when
one is known. No solver ever guesses a missing input: that's a typed error
surfaced to the user, not a silent default.

Units: forces N, moments N·mm, lengths mm, areas mm², section moduli mm³,
moments of inertia mm⁴, stresses MPa (N/mm²), temperatures °C.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable


class AnalysisInputError(ValueError):
    """Missing or non-physical input for the chosen analysis type."""


@dataclass
class SolverOutcome:
    results: dict[str, Any]
    assumptions: list[str] = field(default_factory=list)
    # None when no material limit is known — the case then stays "computed",
    # never a fake "passed".
    passed: bool | None = None


def _positive(inputs: dict, name: str) -> float:
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisInputError(f"Требуется числовой параметр {name!r}")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise AnalysisInputError(f"Параметр {name!r} должен быть положительным")
    return result


def _number(inputs: dict, name: str) -> float:
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AnalysisInputError(f"Требуется числовой параметр {name!r}")
    result = float(value)
    if not math.isfinite(result):
        raise AnalysisInputError(f"Параметр {name!r} должен быть конечным числом")
    return result


def _safety(limit_mpa: float | None, stress_mpa: float) -> float | None:
    if limit_mpa is None or stress_mpa <= 0:
        return None
    return limit_mpa / stress_mpa


def solve_axial_stress(inputs: dict, material) -> SolverOutcome:
    """σ = F / A against the material yield strength."""
    force_n = abs(_number(inputs, "force_n"))
    area_mm2 = _positive(inputs, "area_mm2")
    stress = force_n / area_mm2
    yield_mpa = getattr(material, "yield_strength_mpa", None) if material else None
    factor = _safety(yield_mpa, stress)
    return SolverOutcome(
        results={"stress_mpa": stress, "yield_strength_mpa": yield_mpa, "safety_factor": factor},
        assumptions=[
            "однородное распределение напряжений по сечению (чистое растяжение/сжатие)",
            "концентраторы напряжений не учтены",
        ],
        passed=None if factor is None else factor >= 1,
    )


def solve_bending(inputs: dict, material) -> SolverOutcome:
    """σ = M / W. The moment comes either directly (moment_nmm) or as a
    cantilever tip load (force_n × length_mm)."""
    if "moment_nmm" in inputs:
        moment = abs(_number(inputs, "moment_nmm"))
        moment_note = "изгибающий момент задан явно"
    else:
        force = abs(_number(inputs, "force_n"))
        length = _positive(inputs, "length_mm")
        moment = force * length
        moment_note = "консольная схема: M = F·L (сила на свободном конце)"
    if "section_modulus_mm3" in inputs:
        modulus = _positive(inputs, "section_modulus_mm3")
    elif "diameter_mm" in inputs:
        d = _positive(inputs, "diameter_mm")
        modulus = math.pi * d**3 / 32
    else:
        raise AnalysisInputError("Нужен section_modulus_mm3 или diameter_mm (круглое сечение)")
    stress = moment / modulus
    yield_mpa = getattr(material, "yield_strength_mpa", None) if material else None
    factor = _safety(yield_mpa, stress)
    return SolverOutcome(
        results={
            "moment_nmm": moment,
            "section_modulus_mm3": modulus,
            "stress_mpa": stress,
            "yield_strength_mpa": yield_mpa,
            "safety_factor": factor,
        },
        assumptions=[
            moment_note,
            "линейно-упругий изгиб (гипотеза плоских сечений)",
            "касательные напряжения от поперечной силы не учтены",
        ],
        passed=None if factor is None else factor >= 1,
    )


def solve_torsion(inputs: dict, material) -> SolverOutcome:
    """τ = T / Wp; allowable shear taken as 0.58·σт (von Mises)."""
    torque = abs(_number(inputs, "torque_nmm"))
    if "polar_modulus_mm3" in inputs:
        wp = _positive(inputs, "polar_modulus_mm3")
    elif "diameter_mm" in inputs:
        d = _positive(inputs, "diameter_mm")
        wp = math.pi * d**3 / 16
    else:
        raise AnalysisInputError("Нужен polar_modulus_mm3 или diameter_mm (сплошной круглый вал)")
    shear = torque / wp
    yield_mpa = getattr(material, "yield_strength_mpa", None) if material else None
    shear_limit = 0.58 * yield_mpa if yield_mpa else None
    factor = _safety(shear_limit, shear)
    return SolverOutcome(
        results={
            "polar_modulus_mm3": wp,
            "shear_stress_mpa": shear,
            "shear_limit_mpa": shear_limit,
            "safety_factor": factor,
        },
        assumptions=[
            "сплошной круглый вал, чистое кручение",
            "допускаемое касательное напряжение 0.58·σт (критерий Мизеса)",
        ],
        passed=None if factor is None else factor >= 1,
    )


def solve_buckling(inputs: dict, material) -> SolverOutcome:
    """Euler critical load: Pcr = π²·E·I / (μ·L)²."""
    length = _positive(inputs, "length_mm")
    force = abs(_number(inputs, "force_n"))
    mu = float(inputs.get("end_factor_mu", 1.0))
    if not math.isfinite(mu) or mu <= 0:
        raise AnalysisInputError("Коэффициент приведения длины end_factor_mu должен быть положительным")
    if "moment_inertia_mm4" in inputs:
        inertia = _positive(inputs, "moment_inertia_mm4")
    elif "diameter_mm" in inputs:
        d = _positive(inputs, "diameter_mm")
        inertia = math.pi * d**4 / 64
    else:
        raise AnalysisInputError("Нужен moment_inertia_mm4 или diameter_mm (круглое сечение)")
    modulus = getattr(material, "elastic_modulus_mpa", None) if material else None
    if not modulus:
        raise AnalysisInputError("Для расчёта устойчивости нужен материал с модулем упругости")
    critical = math.pi**2 * modulus * inertia / (mu * length) ** 2
    factor = critical / force if force > 0 else None
    return SolverOutcome(
        results={
            "moment_inertia_mm4": inertia,
            "critical_force_n": critical,
            "applied_force_n": force,
            "safety_factor": factor,
        },
        assumptions=[
            f"формула Эйлера, коэффициент приведения длины μ={mu:g}",
            "идеально прямой упругий стержень, центральное сжатие",
            "применимо только выше предельной гибкости (упругая потеря устойчивости)",
        ],
        passed=None if factor is None else factor >= 1,
    )


def solve_thermal_expansion(inputs: dict, material) -> SolverOutcome:
    """ΔL = α·L·ΔT; when the part is constrained, σ = E·α·ΔT."""
    length = _positive(inputs, "length_mm")
    delta_t = _number(inputs, "delta_t_c")
    alpha = inputs.get("alpha_1_k")
    if alpha is None:
        alpha = getattr(material, "thermal_expansion_1_k", None) if material else None
    if not alpha or not math.isfinite(float(alpha)) or float(alpha) <= 0:
        raise AnalysisInputError("Нужен alpha_1_k или материал с коэффициентом теплового расширения")
    alpha = float(alpha)
    elongation = alpha * length * delta_t
    constrained = bool(inputs.get("constrained", False))
    results: dict[str, Any] = {
        "alpha_1_k": alpha,
        "elongation_mm": elongation,
        "constrained": constrained,
    }
    assumptions = ["линейное тепловое расширение, равномерный нагрев по длине"]
    passed: bool | None = None
    if constrained:
        modulus = getattr(material, "elastic_modulus_mpa", None) if material else None
        if not modulus:
            raise AnalysisInputError("Для защемлённой схемы нужен материал с модулем упругости")
        stress = modulus * alpha * abs(delta_t)
        yield_mpa = getattr(material, "yield_strength_mpa", None) if material else None
        factor = _safety(yield_mpa, stress)
        results.update({"thermal_stress_mpa": stress, "yield_strength_mpa": yield_mpa, "safety_factor": factor})
        assumptions.append("оба конца жёстко защемлены: σ = E·α·ΔT")
        passed = None if factor is None else factor >= 1
    return SolverOutcome(results=results, assumptions=assumptions, passed=passed)


SOLVERS: dict[str, Callable[[dict, Any], SolverOutcome]] = {
    "axial_stress": solve_axial_stress,
    "bending": solve_bending,
    "torsion": solve_torsion,
    "buckling": solve_buckling,
    "thermal_expansion": solve_thermal_expansion,
}
