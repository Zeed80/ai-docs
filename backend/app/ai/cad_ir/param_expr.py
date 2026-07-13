"""A1: safe evaluation of parameter expressions.

A sketch parameter may carry an ``expression`` (e.g. ``width = 2 * height +
5``) instead of a fixed value. This module resolves such expressions in
dependency order with a whitelisted AST evaluator — no ``eval`` of arbitrary
Python — so a parameter table can drive geometry the way a real parametric
CAD does. Expressions reference other parameters and a small set of math
functions/constants only; cycles and unknown names are typed errors.
"""

from __future__ import annotations

import ast
import math

from app.ai.cad_ir.schema import CadParameter


class ParamExprError(ValueError):
    """A parameter expression is malformed, references an unknown name, or the
    parameter table has a dependency cycle — surfaced to the editor, never a
    silent wrong number."""


_CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCS = {
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "sqrt": math.sqrt,
    "abs": abs,
    "min": min,
    "max": max,
    "radians": math.radians,
    "degrees": math.degrees,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
}


def _eval_node(node: ast.AST, names: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, names)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ParamExprError(f"недопустимая константа: {node.value!r}")
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ParamExprError(f"неизвестное имя: {node.id}")
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left, names), _eval_node(node.right, names)
        op = node.op
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            if right == 0:
                raise ParamExprError("деление на ноль")
            return left / right
        if isinstance(op, ast.Mod):
            return left % right
        if isinstance(op, ast.Pow):
            return left**right
        raise ParamExprError("недопустимая операция")
    if isinstance(node, ast.UnaryOp):
        val = _eval_node(node.operand, names)
        if isinstance(node.op, ast.UAdd):
            return +val
        if isinstance(node.op, ast.USub):
            return -val
        raise ParamExprError("недопустимая унарная операция")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ParamExprError("недопустимая функция")
        args = [_eval_node(a, names) for a in node.args]
        try:
            return float(_FUNCS[node.func.id](*args))
        except (TypeError, ValueError) as exc:
            raise ParamExprError(f"ошибка вызова {node.func.id}: {exc}") from exc
    raise ParamExprError("недопустимое выражение")


def _eval_expr(expression: str, names: dict[str, float]) -> float:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ParamExprError(f"синтаксическая ошибка: {exc.msg}") from exc
    return _eval_node(tree, names)


def resolve_parameters(parameters: list[CadParameter]) -> dict[str, float]:
    """Resolve every parameter to a concrete value: expression-carrying ones
    are evaluated in dependency order; plain ones keep their stored value.
    Raises ``ParamExprError`` on cycles or bad references."""
    exprs = {p.name: p.expression for p in parameters if p.expression}
    stored = {p.name: p.value for p in parameters}
    resolved: dict[str, float] = {}

    def resolve(name: str, stack: tuple[str, ...]) -> float:
        if name in resolved:
            return resolved[name]
        if name not in exprs:
            if name not in stored:
                raise ParamExprError(f"неизвестное имя: {name}")
            resolved[name] = stored[name]
            return resolved[name]
        if name in stack:
            raise ParamExprError(
                f"циклическая зависимость параметров: {' → '.join((*stack, name))}"
            )
        tree = ast.parse(exprs[name], mode="eval")
        # gather referenced names and resolve them first
        deps = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        env: dict[str, float] = {}
        for dep in deps:
            if dep in _CONSTS or dep in _FUNCS:
                continue
            env[dep] = resolve(dep, (*stack, name))
        value = _eval_node(tree, env)
        resolved[name] = value
        return value

    for p in parameters:
        resolve(p.name, ())
    return resolved


def apply_parameter_expressions(parameters: list[CadParameter]) -> list[CadParameter]:
    """Return the parameter list with each expression-carrying parameter's
    ``value`` updated to its resolved number (so the stored value and the UI
    always show the computed result). Plain parameters are unchanged."""
    resolved = resolve_parameters(parameters)
    out: list[CadParameter] = []
    for p in parameters:
        if p.expression:
            out.append(p.model_copy(update={"value": resolved[p.name]}))
        else:
            out.append(p)
    return out
