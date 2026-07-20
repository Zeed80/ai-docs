import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "gate_vectorizer_candidate.py"
_SPEC = importlib.util.spec_from_file_location("gate_vectorizer_candidate", _SCRIPT)
gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(gate)


def _report(**overrides):
    metrics = {
        "entity_precision": 0.999,
        "entity_recall": 0.999,
        "entity_f1": 0.999,
        "exact_sheet_rate": 0.995,
        "false_exact_rate": 0.0,
        "dxf_reopen_rate": 1.0,
        "entity_evaluated_files": 100,
    }
    metrics.update(overrides)
    return {"summary": {"dwg": metrics}}


def _evaluate(baseline, candidate):
    return gate.evaluate_candidate(
        baseline,
        candidate,
        min_precision=0.995,
        min_recall=0.995,
        min_exact_sheet_rate=0.99,
    )


def test_exact_candidate_passes() -> None:
    assert _evaluate(_report(entity_f1=0.998), _report()) == []


def test_false_exact_is_always_blocking() -> None:
    failures = _evaluate(_report(), _report(false_exact_rate=0.01))
    assert any("false_exact_rate must be 0" in failure for failure in failures)


def test_legacy_pixel_score_cannot_substitute_for_entity_metrics() -> None:
    candidate = {"summary": {"dwg": {
        "mean_recall": 0.999,
        "mean_precision": 0.999,
        "coverage_ok_rate": 1.0,
    }}}
    failures = _evaluate(_report(), candidate)
    assert failures == [
        "candidate is missing held-out metrics: entity_precision, entity_recall, "
        "entity_f1, exact_sheet_rate, false_exact_rate, dxf_reopen_rate, "
        "entity_evaluated_files"
    ]


def test_semantic_regression_blocks_promotion() -> None:
    failures = _evaluate(
        _report(entity_f1=0.9999),
        _report(entity_f1=0.999),
    )
    assert any("entity_f1 regressed" in failure for failure in failures)
