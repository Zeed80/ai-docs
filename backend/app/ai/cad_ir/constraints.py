"""Deterministic constraint evaluator for the editable CAD IR.

The browser may render a sketch immediately, but engineering trust only rises
after this module resolves every reference and measures residuals.  It is a
small, dependency-free core shared by the current web editor and a future
numeric solver sidecar; no heuristic silently moves recognised geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.ai.cad_ir.schema import CadIR, Circle, GeometricConstraint, Point, Segment


@dataclass(frozen=True)
class ConstraintResult:
    constraint_id: str
    ok: bool
    message: str
    entity_ids: tuple[str, ...]


@dataclass(frozen=True)
class SolveResult:
    converged: bool
    residual: float
    iterations: int
    message: str
    checks: tuple[ConstraintResult, ...]


def _point(ir: CadIR, entity_id: str, point_name: str) -> Point | None:
    entity = ir.entity_by_id(entity_id)
    value = getattr(entity, point_name, None) if entity else None
    return value if isinstance(value, Point) else None


def _entities(ir: CadIR, ids: list[str]):
    return [ir.entity_by_id(entity_id) for entity_id in ids]


def _target(constraint: GeometricConstraint, parameters: dict[str, float]) -> float | None:
    if constraint.parameter:
        return parameters.get(constraint.parameter)
    return constraint.value


def _segment_vector(segment: Segment) -> tuple[float, float]:
    return segment.p2.x - segment.p1.x, segment.p2.y - segment.p1.y


def _length(segment: Segment) -> float:
    return math.hypot(segment.p2.x - segment.p1.x, segment.p2.y - segment.p1.y)


def evaluate_constraints(ir: CadIR) -> list[ConstraintResult]:
    parameters = {item.name: item.value for item in ir.parameters}
    results: list[ConstraintResult] = []
    for c in ir.constraints:
        if not c.enabled:
            continue
        ids = tuple([ref.entity_id for ref in c.refs] or c.entity_ids)
        if c.driven:
            # A driven (reference) dimension measures geometry, it doesn't
            # constrain it — always satisfied by definition.
            results.append(ConstraintResult(c.id, True, "справочный размер", ids))
            continue
        target = _target(c, parameters)
        if c.parameter and target is None:
            results.append(ConstraintResult(c.id, False, f"Параметр {c.parameter} не найден", ids))
            continue
        try:
            if c.kind == "coincident" and len(c.refs) == 2:
                a, b = (_point(ir, ref.entity_id, ref.point) for ref in c.refs)
                residual = math.inf if not a or not b else math.hypot(a.x - b.x, a.y - b.y)
            elif c.kind in ("horizontal", "vertical") and len(c.entity_ids) == 1:
                segment = ir.entity_by_id(c.entity_ids[0])
                if not isinstance(segment, Segment):
                    raise ValueError("ограничение применимо только к отрезку")
                residual = abs(segment.p2.y - segment.p1.y) if c.kind == "horizontal" else abs(segment.p2.x - segment.p1.x)
            elif c.kind in ("parallel", "perpendicular") and len(c.entity_ids) == 2:
                first, second = _entities(ir, c.entity_ids)
                if not isinstance(first, Segment) or not isinstance(second, Segment):
                    raise ValueError("ограничение применимо только к отрезкам")
                ax, ay = _segment_vector(first)
                bx, by = _segment_vector(second)
                denominator = max(_length(first) * _length(second), 1e-12)
                dot = (ax * bx + ay * by) / denominator
                cross = (ax * by - ay * bx) / denominator
                residual = abs(cross) if c.kind == "parallel" else abs(dot)
            elif c.kind == "concentric" and len(c.entity_ids) == 2:
                first, second = _entities(ir, c.entity_ids)
                if not isinstance(first, Circle) or not isinstance(second, Circle):
                    raise ValueError("соосность применима только к окружностям")
                residual = math.hypot(first.center.x - second.center.x, first.center.y - second.center.y)
            elif c.kind == "equal" and len(c.entity_ids) == 2:
                first, second = _entities(ir, c.entity_ids)
                if isinstance(first, Segment) and isinstance(second, Segment):
                    residual = abs(_length(first) - _length(second))
                elif isinstance(first, Circle) and isinstance(second, Circle):
                    residual = abs(first.radius - second.radius)
                else:
                    raise ValueError("равенство требует двух отрезков или двух окружностей")
            elif c.kind == "distance" and len(c.refs) == 2 and target is not None:
                a, b = (_point(ir, ref.entity_id, ref.point) for ref in c.refs)
                residual = math.inf if not a or not b else abs(math.hypot(a.x - b.x, a.y - b.y) - target)
            elif c.kind in ("radius", "diameter") and len(c.entity_ids) == 1 and target is not None:
                circle = ir.entity_by_id(c.entity_ids[0])
                if not isinstance(circle, Circle):
                    raise ValueError("радиус/диаметр применим только к окружности")
                actual = circle.radius if c.kind == "radius" else circle.radius * 2
                residual = abs(actual - target)
            elif c.kind == "angle" and len(c.entity_ids) == 2 and target is not None:
                first, second = _entities(ir, c.entity_ids)
                if not isinstance(first, Segment) or not isinstance(second, Segment):
                    raise ValueError("угол применим только к отрезкам")
                ax, ay = _segment_vector(first)
                bx, by = _segment_vector(second)
                angle = abs(math.degrees(math.atan2(ax * by - ay * bx, ax * bx + ay * by)))
                residual = abs(angle - target)
            else:
                raise ValueError("недостаточно или неверно заданы ссылки ограничения")
        except ValueError as exc:
            results.append(ConstraintResult(c.id, False, str(exc), ids))
            continue
        results.append(ConstraintResult(c.id, residual <= c.tolerance, f"остаток {residual:.6g}", ids))
    return results


# Scalar-equation count each supported constraint contributes to the system —
# used for DOF/over-constrained analysis (a point-on-point ties both axes).
_EQUATIONS_PER_KIND = {"coincident": 2, "concentric": 2}


def _equation_count(constraint: GeometricConstraint) -> int:
    return _EQUATIONS_PER_KIND.get(constraint.kind, 1)


def _build_system(ir: CadIR, active: list[GeometricConstraint], *, only: set[str] | None = None):
    """Shared residual system for the solver and the DOF analyzer.

    Returns ``(variables, values, residuals_fn)`` where variables are the
    editable (entity_id, coord) pairs, values their current numbers, and
    residuals_fn maps a value-vector to the constraint residual list. ``only``
    restricts the variable set to the given entity ids (the constrained
    subsystem) — the analyzer wants that, the solver passes None for all."""
    parameter_values = {item.name: item.value for item in ir.parameters}
    variables: list[tuple[str, str]] = []
    values: list[float] = []
    for entity in ir.entities:
        if only is not None and entity.id not in only:
            continue
        if isinstance(entity, Segment):
            for name in ("p1.x", "p1.y", "p2.x", "p2.y"):
                point_name, axis = name.split(".")
                variables.append((entity.id, name))
                values.append(getattr(getattr(entity, point_name), axis))
        elif isinstance(entity, Circle):
            for name in ("center.x", "center.y", "radius"):
                variables.append((entity.id, name))
                values.append(entity.radius if name == "radius" else getattr(entity.center, name[-1]))

    index = {key: position for position, key in enumerate(variables)}

    def coordinate(vector, entity_id: str, name: str) -> float | None:
        return vector[index[(entity_id, name)]] if (entity_id, name) in index else None

    def point(vector, ref) -> tuple[float, float] | None:
        x = coordinate(vector, ref.entity_id, f"{ref.point}.x")
        y = coordinate(vector, ref.entity_id, f"{ref.point}.y")
        return (x, y) if x is not None and y is not None else None

    def segment(vector, entity_id: str):
        data = [coordinate(vector, entity_id, name) for name in ("p1.x", "p1.y", "p2.x", "p2.y")]
        return data if all(item is not None for item in data) else None

    def circle(vector, entity_id: str):
        data = [coordinate(vector, entity_id, name) for name in ("center.x", "center.y", "radius")]
        return data if all(item is not None for item in data) else None

    def target(constraint: GeometricConstraint) -> float | None:
        return parameter_values.get(constraint.parameter) if constraint.parameter else constraint.value

    def residuals(vector) -> list[float]:
        out: list[float] = []
        for c in active:
            try:
                if c.kind == "coincident" and len(c.refs) == 2:
                    a, b = point(vector, c.refs[0]), point(vector, c.refs[1])
                    if not a or not b: raise ValueError
                    out.extend((a[0] - b[0], a[1] - b[1]))
                elif c.kind in ("horizontal", "vertical") and len(c.entity_ids) == 1:
                    s = segment(vector, c.entity_ids[0])
                    if not s: raise ValueError
                    out.append(s[3] - s[1] if c.kind == "horizontal" else s[2] - s[0])
                elif c.kind in ("parallel", "perpendicular", "angle") and len(c.entity_ids) == 2:
                    a, b = segment(vector, c.entity_ids[0]), segment(vector, c.entity_ids[1])
                    if not a or not b: raise ValueError
                    ax, ay, bx, by = a[2] - a[0], a[3] - a[1], b[2] - b[0], b[3] - b[1]
                    scale = max(math.hypot(ax, ay) * math.hypot(bx, by), 1e-9)
                    if c.kind == "parallel": out.append((ax * by - ay * bx) / scale)
                    elif c.kind == "perpendicular": out.append((ax * bx + ay * by) / scale)
                    else:
                        wanted = target(c)
                        if wanted is None: raise ValueError
                        angle = math.degrees(math.atan2(ax * by - ay * bx, ax * bx + ay * by))
                        out.append((angle - wanted) / 10.0)
                elif c.kind == "concentric" and len(c.entity_ids) == 2:
                    a, b = circle(vector, c.entity_ids[0]), circle(vector, c.entity_ids[1])
                    if not a or not b: raise ValueError
                    out.extend((a[0] - b[0], a[1] - b[1]))
                elif c.kind == "equal" and len(c.entity_ids) == 2:
                    a_segment, b_segment = segment(vector, c.entity_ids[0]), segment(vector, c.entity_ids[1])
                    a_circle, b_circle = circle(vector, c.entity_ids[0]), circle(vector, c.entity_ids[1])
                    if a_segment and b_segment:
                        out.append(math.hypot(a_segment[2] - a_segment[0], a_segment[3] - a_segment[1]) - math.hypot(b_segment[2] - b_segment[0], b_segment[3] - b_segment[1]))
                    elif a_circle and b_circle: out.append(a_circle[2] - b_circle[2])
                    else: raise ValueError
                elif c.kind == "distance" and len(c.refs) == 2 and target(c) is not None:
                    a, b = point(vector, c.refs[0]), point(vector, c.refs[1])
                    if not a or not b: raise ValueError
                    out.append(math.hypot(a[0] - b[0], a[1] - b[1]) - target(c))
                elif c.kind in ("radius", "diameter") and len(c.entity_ids) == 1 and target(c) is not None:
                    item = circle(vector, c.entity_ids[0])
                    if not item: raise ValueError
                    out.append((item[2] if c.kind == "radius" else item[2] * 2) - target(c))
                else:
                    raise ValueError
            except ValueError:
                # A large finite residual makes malformed constraints visible
                # in the solver report without crashing an editor session.
                out.append(1e6)
        return out or [0.0]

    return variables, values, residuals


def solve_constraints(ir: CadIR, *, max_nfev: int = 200) -> SolveResult:
    """Numerically satisfy the supported 2D constraints in-place.

    The solver is invoked only through an explicit editor action.  It never
    invents topology, deletes entities, or applies ambiguous 3D hypotheses;
    it only moves sketch coordinates/radii to the nearest configuration that
    satisfies declared constraints and named driving parameters.
    """
    active = [c for c in ir.constraints if c.enabled and not c.driven]
    if not active:
        return SolveResult(True, 0.0, 0, "Ограничения отсутствуют", ())
    try:
        from scipy.optimize import least_squares
    except ImportError as exc:  # pragma: no cover - dependency is production-required
        raise RuntimeError("Для решения ограничений требуется scipy") from exc

    variables, values, residuals = _build_system(ir, active)
    if not variables:
        return SolveResult(False, math.inf, 0, "Нет редактируемой геометрии для ограничений", tuple(evaluate_constraints(ir)))

    solved = least_squares(residuals, values, max_nfev=max_nfev, xtol=1e-10, ftol=1e-10, gtol=1e-10)
    for (entity_id, name), value in zip(variables, solved.x, strict=True):
        entity = ir.entity_by_id(entity_id)
        if not entity:
            continue
        if name == "radius":
            entity.radius = max(float(value), 1e-9)
        else:
            point_name, axis = name.split(".")
            setattr(getattr(entity, point_name), axis, float(value))
    checks = tuple(evaluate_constraints(ir))
    residual = max((abs(item) for item in residuals(solved.x)), default=0.0)
    converged = bool(solved.success and all(check.ok for check in checks))
    return SolveResult(converged, residual, int(solved.nfev), str(solved.message), checks)


@dataclass(frozen=True)
class DofReport:
    """Degrees-of-freedom analysis of the constrained subsystem (A1). ``dof``
    is how many free motions remain (0 = fully constrained); ``redundant`` is
    true when constraints duplicate information (rank < equations); ``conflict``
    is true when an enabled constraint can't be satisfied at the current
    geometry."""

    unknowns: int
    equations: int
    rank: int
    dof: int
    state: str  # "unconstrained" | "under_constrained" | "well_constrained" | "over_constrained"
    redundant: bool
    conflict: bool


def analyze_constraints(ir: CadIR) -> DofReport:
    """Numerically estimate the sketch's degrees of freedom from the Jacobian
    rank of the enabled constraints over the entities they touch."""
    active = [c for c in ir.constraints if c.enabled and not c.driven]
    involved: set[str] = set()
    for c in active:
        involved.update(c.entity_ids)
        involved.update(r.entity_id for r in c.refs)
    if not active or not involved:
        return DofReport(0, 0, 0, 0, "unconstrained", False, False)

    variables, values, residuals = _build_system(ir, active, only=involved)
    unknowns = len(variables)
    equations = sum(_equation_count(c) for c in active)
    conflict = any(not chk.ok for chk in evaluate_constraints(ir))

    rank = 0
    if unknowns:
        import numpy as np

        x0 = np.asarray(values, dtype=float)
        r0 = np.asarray(residuals(x0), dtype=float)
        eps = 1e-6
        jac = np.zeros((len(r0), unknowns))
        for j in range(unknowns):
            xp = x0.copy()
            xp[j] += eps
            jac[:, j] = (np.asarray(residuals(xp), dtype=float) - r0) / eps
        rank = int(np.linalg.matrix_rank(jac, tol=1e-6))

    dof = max(unknowns - rank, 0)
    redundant = rank < equations
    if dof > 0:
        state = "under_constrained"
    elif redundant:
        state = "over_constrained"
    else:
        state = "well_constrained"
    return DofReport(unknowns, equations, rank, dof, state, redundant, conflict)
