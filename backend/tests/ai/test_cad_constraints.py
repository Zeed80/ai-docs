"""Constraint residuals are part of the deterministic CAD validation gate."""

from app.ai.cad_ir import CadIR, Circle, Segment, SourceInfo
from app.ai.cad_ir.constraints import analyze_constraints, solve_constraints
from app.ai.cad_ir.schema import CadParameter, GeometricConstraint, Point, SketchPointRef
from app.ai.cad_validate import validate_ir


def _ir(*entities):
    return CadIR(source=SourceInfo(image_width=100, image_height=100, kind="blank"), scale=1, scale_source="manual", entities=list(entities))


def test_dof_of_single_horizontal_segment():
    # a segment has 4 DOF; one horizontal constraint removes 1 → 3 remain.
    s = Segment(id="a", p1=Point(x=0, y=0), p2=Point(x=100, y=5))
    ir = _ir(s)
    ir.constraints = [GeometricConstraint(kind="horizontal", entity_ids=["a"])]
    report = analyze_constraints(ir)
    assert report.unknowns == 4
    assert report.equations == 1
    assert report.dof == 3
    assert report.state == "under_constrained"


def test_dof_reports_redundant_constraints():
    # horizontal applied twice to the same segment: 2 equations, rank 1.
    s = Segment(id="a", p1=Point(x=0, y=0), p2=Point(x=100, y=0))
    ir = _ir(s)
    ir.constraints = [
        GeometricConstraint(kind="horizontal", entity_ids=["a"]),
        GeometricConstraint(kind="horizontal", entity_ids=["a"]),
    ]
    report = analyze_constraints(ir)
    assert report.equations == 2
    assert report.rank == 1
    assert report.redundant is True


def test_dof_no_active_constraints_is_unconstrained():
    s = Segment(id="a", p1=Point(x=0, y=0), p2=Point(x=100, y=0))
    report = analyze_constraints(_ir(s))
    assert report.state == "unconstrained"
    assert report.dof == 0


def test_satisfied_constraints_leave_no_constraint_errors():
    first = Segment(id="a", p1=Point(x=0, y=0), p2=Point(x=10, y=0))
    second = Segment(id="b", p1=Point(x=10, y=0), p2=Point(x=10, y=10))
    ir = _ir(first, second)
    ir.constraints = [
        GeometricConstraint(kind="coincident", refs=[SketchPointRef(entity_id="a", point="p2"), SketchPointRef(entity_id="b", point="p1")]),
        GeometricConstraint(kind="horizontal", entity_ids=["a"]),
        GeometricConstraint(kind="vertical", entity_ids=["b"]),
        GeometricConstraint(kind="distance", refs=[SketchPointRef(entity_id="a", point="p1"), SketchPointRef(entity_id="a", point="p2")], parameter="width"),
    ]
    ir.parameters = [CadParameter(name="width", value=10)]
    report = validate_ir(ir)
    assert not [issue for issue in report.issues if issue.code.startswith("CONSTRAINT_")]


def test_unsatisfied_constraint_is_a_blocking_error_with_entity_refs():
    circle = Circle(id="c", center=Point(x=10, y=10), radius=4)
    ir = _ir(circle)
    ir.constraints = [GeometricConstraint(kind="diameter", entity_ids=["c"], value=10)]
    report = validate_ir(ir)
    issue = next(issue for issue in report.issues if issue.code == "CONSTRAINT_UNSATISFIED")
    assert issue.severity == "error"
    assert issue.entity_ids == ["c"]


def test_missing_constraint_parameter_is_blocking():
    circle = Circle(id="c", center=Point(x=10, y=10), radius=4)
    ir = _ir(circle)
    ir.constraints = [GeometricConstraint(kind="radius", entity_ids=["c"], parameter="diameter")]
    report = validate_ir(ir)
    assert any(issue.code == "CONSTRAINT_REFERENCE_INVALID" for issue in report.issues)


def test_solver_rebuilds_circle_from_named_diameter_parameter():
    circle = Circle(id="c", center=Point(x=10, y=10), radius=2)
    ir = _ir(circle)
    ir.parameters = [CadParameter(name="diameter", value=16)]
    ir.constraints = [GeometricConstraint(kind="diameter", entity_ids=["c"], parameter="diameter")]
    result = solve_constraints(ir)
    assert result.converged
    assert abs(circle.radius - 8) < 1e-6
