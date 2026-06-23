"""Safe spreadsheet formula engine (server side).

Evaluates Excel-like formulas for ad-hoc sheets and computed spec-table columns.
Pure computation only — no I/O, imports, attribute access or arbitrary calls;
expressions are parsed to an AST and walked against a strict node/function
whitelist. Mirrors the function set used by the frontend HyperFormula engine so
on-screen and exported/headless results agree.

Supported:
- arithmetic ``+ - * / %`` and comparisons, parentheses;
- column references by key (``quantity``) → the current row's value;
- A1-style cell refs (``A1``) and ranges (``A1:A10``) where the letter is the
  column position (A = first column);
- functions: SUM, AVERAGE/AVG, MIN, MAX, COUNT, ROUND, ABS, IF, AND, OR, NOT.

Dependency resolution is lazy with memoisation and cycle detection, so a
computed column may reference other computed columns/cells (``vat = amount*0.2``
where ``amount = quantity*unit_price``).
"""

from __future__ import annotations

import ast
import re
from typing import Any

_MAX_FORMULA_LEN = 1000
_COL_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

_RANGE_RE = re.compile(r"\b([A-Za-z]+\d+):([A-Za-z]+\d+)\b")
_CELL_RE = re.compile(r"\b([A-Za-z]+\d+)\b")
_CELLREF_RE = re.compile(r"^([A-Za-z]+)(\d+)$")
_STR_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def _to_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return 0.0


def _flatten(args: tuple) -> list[float]:
    out: list[float] = []
    for a in args:
        if isinstance(a, (list, tuple)):
            out.extend(_to_number(x) for x in a)
        else:
            out.append(_to_number(a))
    return out


def _f_sum(*args):
    return sum(_flatten(args))


def _f_avg(*args):
    nums = _flatten(args)
    return sum(nums) / len(nums) if nums else 0.0


def _f_min(*args):
    nums = _flatten(args)
    return min(nums) if nums else 0.0


def _f_max(*args):
    nums = _flatten(args)
    return max(nums) if nums else 0.0


def _f_count(*args):
    return float(len(_flatten(args)))


def _f_round(value, digits=0):
    return round(_to_number(value), int(_to_number(digits)))


def _f_abs(value):
    return abs(_to_number(value))


def _f_if(cond, a, b=0):
    return a if cond else b


def _f_and(*args):
    return all(bool(a) for a in args)


def _f_or(*args):
    return any(bool(a) for a in args)


def _f_not(a):
    return not bool(a)


_FUNCTIONS = {
    "SUM": _f_sum, "AVERAGE": _f_avg, "AVG": _f_avg, "MIN": _f_min, "MAX": _f_max,
    "COUNT": _f_count, "ROUND": _f_round, "ABS": _f_abs, "IF": _f_if,
    "AND": _f_and, "OR": _f_or, "NOT": _f_not,
}

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.Call, ast.Constant, ast.Name, ast.Load, ast.List, ast.Tuple,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.USub, ast.UAdd,
    ast.And, ast.Or, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def _validate(node: ast.AST) -> None:
    for child in ast.walk(node):
        if not isinstance(child, _ALLOWED_NODES):
            raise ValueError(f"Недопустимая конструкция: {type(child).__name__}")
        if isinstance(child, ast.Call) and not isinstance(child.func, ast.Name):
            raise ValueError("Недопустимый вызов")


class FormulaEngine:
    """Lazy, memoised evaluator over a column/row grid."""

    def __init__(self, columns: list[dict], rows: list[dict]):
        self.columns = columns
        self.rows = rows
        self.col_keys = [c.get("key") for c in columns]
        self.key_set = {k for k in self.col_keys if k}
        self.letter_to_key = {
            _COL_LETTERS[i]: k for i, k in enumerate(self.col_keys) if i < 26 and k
        }
        self.col_formula = {
            c.get("key"): c.get("formula") for c in columns if c.get("formula")
        }
        self._memo: dict[tuple[str, int], Any] = {}
        self._in_progress: set[tuple[str, int]] = set()

    # ── reference resolution ────────────────────────────────────────────────

    def value(self, key: str, row_index: int) -> Any:
        """Computed-or-raw value of one cell, resolving formulas lazily."""
        if not key or row_index < 0 or row_index >= len(self.rows):
            return None
        cache_key = (key, row_index)
        if cache_key in self._memo:
            return self._memo[cache_key]
        if cache_key in self._in_progress:
            return "#CYCLE"
        raw = self.rows[row_index].get(key)
        formula = None
        if isinstance(raw, str) and raw.startswith("="):
            formula = raw
        elif key in self.col_formula:
            f = self.col_formula[key]
            formula = f if str(f).startswith("=") else "=" + str(f)
        if formula is None:
            self._memo[cache_key] = raw
            return raw
        self._in_progress.add(cache_key)
        result = self._eval(formula, row_index)
        self._in_progress.discard(cache_key)
        self._memo[cache_key] = result
        return result

    def _col(self, key: str, row_index: int) -> float:
        return _to_number(self.value(key, row_index))

    def _cell(self, ref: str) -> float:
        m = _CELLREF_RE.match(ref)
        if not m:
            return 0.0
        key = self.letter_to_key.get(m.group(1).upper())
        num = int(m.group(2))
        if key is None:
            return 0.0
        return _to_number(self.value(key, num - 1))

    def _range(self, a: str, b: str) -> list[float]:
        ma, mb = _CELLREF_RE.match(a), _CELLREF_RE.match(b)
        if not ma or not mb:
            return []
        key = self.letter_to_key.get(ma.group(1).upper())
        if key is None:
            return []
        lo, hi = sorted((int(ma.group(2)), int(mb.group(2))))
        return [_to_number(self.value(key, n - 1)) for n in range(lo, hi + 1)
                if 1 <= n <= len(self.rows)]

    # ── compilation ─────────────────────────────────────────────────────────

    def _transform(self, expr: str) -> str:
        """Rewrite refs into safe calls; stash every replacement so later passes
        never re-scan generated tokens or quoted content."""
        stash: list[str] = []

        def _put(text: str) -> str:
            stash.append(text)
            return f"\x00{len(stash) - 1}\x00"

        body = _STR_RE.sub(lambda m: _put(m.group(0)), expr)
        body = _RANGE_RE.sub(lambda m: _put(f"__range('{m.group(1)}','{m.group(2)}')"), body)
        body = _CELL_RE.sub(lambda m: _put(f"__cell('{m.group(1)}')"), body)

        def _ident(m: re.Match) -> str:
            name = m.group(0)
            nxt = body[m.end():m.end() + 1]
            if name in _FUNCTIONS or nxt == "(":
                return name
            if name in self.key_set:
                return _put(f"__col('{name}')")
            return name

        body = _IDENT_RE.sub(_ident, body)

        # Restore stashed fragments (they may nest, so loop until stable).
        while "\x00" in body:
            body = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], body)
        return body

    def _eval(self, formula: str, row_index: int) -> Any:
        expr = formula[1:] if formula.startswith("=") else formula
        if len(expr) > _MAX_FORMULA_LEN:
            return "#TOOLONG"
        try:
            transformed = self._transform(expr)
            tree = ast.parse(transformed, mode="eval")
            _validate(tree)
            names = {
                "__col": lambda k: self._col(k, row_index),
                "__cell": self._cell,
                "__range": self._range,
                **_FUNCTIONS,
            }
            result = eval(  # noqa: S307 — AST whitelisted, builtins stripped
                compile(tree, "<formula>", "eval"), {"__builtins__": {}}, names
            )
            if isinstance(result, bool):
                return result
            if isinstance(result, float):
                return int(result) if result.is_integer() else round(result, 6)
            return result
        except ZeroDivisionError:
            return "#DIV/0"
        except Exception:
            return "#ERR"


def evaluate_sheet(columns: list[dict], rows: list[dict]) -> list[dict]:
    """Return rows with formula cells/columns resolved to values.

    A cell whose raw value starts with ``=`` is a per-cell formula; a column with
    a ``formula`` attribute is a computed column applied to every row. Non-formula
    cells pass through unchanged.
    """
    engine = FormulaEngine(columns, rows)
    out: list[dict] = []
    for ri, row in enumerate(rows):
        new_row = dict(row)
        for key in engine.col_keys:
            if not key:
                continue
            raw = row.get(key)
            if (isinstance(raw, str) and raw.startswith("=")) or key in engine.col_formula:
                new_row[key] = engine.value(key, ri)
        out.append(new_row)
    return out
