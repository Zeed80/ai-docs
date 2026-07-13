"""A1: parameter-expression resolution (dependency order, safe evaluator)."""

import pytest

from app.ai.cad_ir.param_expr import (
    ParamExprError,
    apply_parameter_expressions,
    resolve_parameters,
)
from app.ai.cad_ir.schema import CadParameter


def _p(name, value=0.0, expression=None):
    return CadParameter(name=name, value=value, expression=expression)


def test_transitive_expression_chain():
    params = [
        _p("height", 20),
        _p("width", expression="2*height + 5"),
        _p("area", expression="width*height"),
    ]
    resolved = resolve_parameters(params)
    assert resolved["width"] == 45.0
    assert resolved["area"] == 900.0


def test_apply_writes_computed_value_back():
    applied = apply_parameter_expressions([_p("h", 10), _p("w", expression="h*3")])
    assert {p.name: p.value for p in applied} == {"h": 10.0, "w": 30.0}


def test_cycle_is_rejected():
    with pytest.raises(ParamExprError):
        resolve_parameters([_p("a", expression="b+1"), _p("b", expression="a+1")])


def test_unknown_name_is_rejected():
    with pytest.raises(ParamExprError):
        resolve_parameters([_p("x", expression="nope + 1")])


def test_math_functions_and_constants():
    resolved = resolve_parameters([_p("d", 10), _p("c", expression="sqrt(d*d) + pi")])
    assert resolved["c"] == pytest.approx(10 + 3.14159265, abs=1e-6)


def test_arbitrary_python_is_rejected():
    # no attribute access / calls outside the whitelist
    with pytest.raises(ParamExprError):
        resolve_parameters([_p("x", expression="__import__('os').getpid()")])
