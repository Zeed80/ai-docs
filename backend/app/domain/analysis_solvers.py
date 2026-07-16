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


# F2: bumped whenever any formula/threshold below changes — stored in every
# run snapshot so an old result can be traced to the exact solver revision
# that produced it.
SOLVER_VERSION = "1.2.0"


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


def _beam_system(
    n_elements: int,
    length: float,
    ei: float,
    supports: str,
    loads: list[dict],
):
    """Assemble and solve the Euler-Bernoulli beam FE system (Hermite
    2-node elements, DOFs [v, θ] per node). Returns nodal DOF vector."""
    import numpy as np

    n_nodes = n_elements + 1
    le = length / n_elements
    k_e = (ei / le**3) * np.array([
        [12, 6 * le, -12, 6 * le],
        [6 * le, 4 * le**2, -6 * le, 2 * le**2],
        [-12, -6 * le, 12, -6 * le],
        [6 * le, 2 * le**2, -6 * le, 4 * le**2],
    ])
    ndof = 2 * n_nodes
    stiffness = np.zeros((ndof, ndof))
    for element in range(n_elements):
        i = 2 * element
        stiffness[i:i + 4, i:i + 4] += k_e

    load_vector = np.zeros(ndof)
    for load in loads:
        kind = load.get("type", "point")
        if kind == "point":
            force = float(load["force_n"])
            position = float(load.get("position_mm", length))
            position = min(max(position, 0.0), length)
            element = min(int(position // le), n_elements - 1)
            xi = (position - element * le) / le
            # Hermite shape functions distribute the point load consistently
            h = np.array([
                1 - 3 * xi**2 + 2 * xi**3,
                le * (xi - 2 * xi**2 + xi**3),
                3 * xi**2 - 2 * xi**3,
                le * (-(xi**2) + xi**3),
            ])
            load_vector[2 * element:2 * element + 4] += force * h
        elif kind == "udl":
            q = float(load["force_n_per_mm"])
            fe = np.array([q * le / 2, q * le**2 / 12, q * le / 2, -q * le**2 / 12])
            for element in range(n_elements):
                load_vector[2 * element:2 * element + 4] += fe
        else:
            raise AnalysisInputError(f"Неизвестный тип нагрузки: {kind!r} (point | udl)")

    if supports == "cantilever":
        fixed = [0, 1]  # v(0) = θ(0) = 0
    elif supports == "simply_supported":
        fixed = [0, ndof - 2]  # v(0) = v(L) = 0
    else:
        raise AnalysisInputError("supports должен быть cantilever или simply_supported")
    free = [dof for dof in range(ndof) if dof not in fixed]
    solution = np.zeros(ndof)
    solution[free] = np.linalg.solve(stiffness[np.ix_(free, free)], load_vector[free])
    # element end moments from k_e @ u_e (θ-DOF rows) — bending moment at nodes
    moments = np.zeros(n_nodes)
    for element in range(n_elements):
        forces = k_e @ solution[2 * element:2 * element + 4]
        moments[element] = max(abs(moments[element]), abs(forces[1]))
        moments[element + 1] = max(abs(moments[element + 1]), abs(forces[3]))
    return solution, moments


def solve_fea_beam(inputs: dict, material) -> SolverOutcome:
    """F3: a real finite-element solve of a 1D Euler-Bernoulli beam — loads,
    restraints, a mesh that is refined until the tip answer stops moving. A
    non-converged mesh FAILS the case (blocking release), it is not rounded
    into an answer."""
    length = _positive(inputs, "length_mm")
    modulus = getattr(material, "elastic_modulus_mpa", None) if material else None
    if not modulus:
        raise AnalysisInputError("Для FEA нужен материал с модулем упругости")
    if "moment_inertia_mm4" in inputs:
        inertia = _positive(inputs, "moment_inertia_mm4")
        section_modulus = _positive(inputs, "section_modulus_mm3")
    elif "diameter_mm" in inputs:
        d = _positive(inputs, "diameter_mm")
        inertia = math.pi * d**4 / 64
        section_modulus = math.pi * d**3 / 32
    else:
        raise AnalysisInputError("Нужен diameter_mm или moment_inertia_mm4 + section_modulus_mm3")
    loads = inputs.get("loads")
    if not isinstance(loads, list) or not loads:
        raise AnalysisInputError("Нужен список loads: [{type: point|udl, ...}]")
    supports = str(inputs.get("supports", "cantilever"))

    ei = modulus * inertia
    tolerance = 0.005
    max_elements = 256
    previous: float | None = None
    converged = False
    n = 4
    deflection = 0.0
    moments = None
    solution = None
    while n <= max_elements:
        solution, moments = _beam_system(n, length, ei, supports, loads)
        deflection = float(max(abs(solution[0::2])))
        if previous is not None and (
            deflection == 0.0 or abs(deflection - previous) / max(abs(deflection), 1e-12) < tolerance
        ):
            converged = True
            break
        previous = deflection
        n *= 2
    max_moment = float(max(abs(moments))) if moments is not None else 0.0
    stress = max_moment / section_modulus
    yield_mpa = getattr(material, "yield_strength_mpa", None) if material else None
    factor = _safety(yield_mpa, stress)
    results = {
        "max_deflection_mm": deflection,
        "max_moment_nmm": max_moment,
        "max_stress_mpa": stress,
        "yield_strength_mpa": yield_mpa,
        "safety_factor": factor,
        "mesh_elements": n if converged else max_elements,
        "converged": converged,
        "convergence_tolerance": tolerance,
    }
    assumptions = [
        "балка Эйлера-Бернулли (сдвиговые деформации не учтены)",
        f"схема опор: {supports}",
        "линейно-упругий материал, малые перемещения",
        f"сетка уточнялась до сходимости прогиба <{tolerance:.1%}",
    ]
    if not converged:
        # a non-converged solve is a FAILED case, never an answer
        return SolverOutcome(results=results, assumptions=assumptions, passed=False)
    passed = None if factor is None else factor >= 1
    return SolverOutcome(results=results, assumptions=assumptions, passed=passed)


SOLVERS: dict[str, Callable[[dict, Any], SolverOutcome]] = {
    "axial_stress": solve_axial_stress,
    "bending": solve_bending,
    "torsion": solve_torsion,
    "buckling": solve_buckling,
    "thermal_expansion": solve_thermal_expansion,
    "fea_beam": solve_fea_beam,
}
