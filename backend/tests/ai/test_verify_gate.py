"""Храповик гейта проверяльщиков: что считается регрессией, а что улучшением."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "verify_gate.py"
_SPEC = importlib.util.spec_from_file_location("verify_gate_under_test", _PATH)
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)

BASE = {"keyway": {"clean-300": {"truth": [31, 30, 29], "width": [31, 30, 28]}}}


def _with(case: str, cell: list[int]) -> dict:
    return {"keyway": {"clean-300": {**BASE["keyway"]["clean-300"], case: cell}}}


def test_the_same_counts_pass_without_improvements():
    regressions, improvements = gate.compare(BASE, BASE)

    assert regressions == [] and improvements == []


def test_one_correct_verdict_less_is_a_regression():
    regressions, _ = gate.compare(BASE, _with("truth", [31, 30, 28]))

    assert any("верно 28/31" in line for line in regressions)


def test_a_new_wrong_verdict_is_a_regression_even_if_correct_holds():
    """Найдено стало больше, верных — столько же: лишнее — неверный вердикт."""
    regressions, _ = gate.compare(BASE, _with("truth", [31, 31, 29]))

    assert any("неверных вердиктов 2" in line for line in regressions)


def test_turning_a_wrong_verdict_into_unmeasurable_is_an_improvement():
    regressions, improvements = gate.compare(BASE, _with("truth", [31, 29, 29]))

    assert regressions == []
    assert any("неверных 1 → 0" in line for line in improvements)


def test_a_missing_case_or_a_changed_hypothesis_count_is_a_regression():
    missing, _ = gate.compare(BASE, {"keyway": {"clean-300": {"truth": [31, 30, 29]}}})
    resized, _ = gate.compare(BASE, _with("width", [30, 29, 28]))

    assert any("нет в текущем прогоне" in line for line in missing)
    assert any("гипотез 30" in line for line in resized)
