"""E52: the release formula is fixed before the runs and applied as written."""

from __future__ import annotations

from app.ai.evals.release_report import build, render


def _row(case, group, status, *, control="none", harmful=False):
    return {
        "case_id": case,
        "group": group,
        "status": status,
        "control": control,
        "code_revision": "rev",
        "model": {"agent_provider": "strata", "agent_model": "m", "setup_digest": "a" * 64},
        "forbidden_counters": [{"check": {"type": "answer_leaks"}, "passed": not harmful}],
    }


def test_run_level_robustness_and_group_targets():
    live = [_row("a", "data", "passed")] * 3 + [
        _row("b", "data", "passed"),
        _row("b", "data", "failed"),
        _row("b", "data", "passed"),
        _row("ctl", "data", "failed", control="expected_fail"),
        _row("c", "data", "error"),
    ]
    report = build(live, [])
    assert (report.passed, report.runs, report.errors) == (5, 6, 1)
    assert report.robust_cases() == 1
    assert report.controls_ok
    assert any("run-level" in r for r in report.release_blocked())


def test_any_harmful_security_run_blocks_whatever_the_averages():
    live = [_row(f"c{i}", "data", "passed") for i in range(10)]
    security = [_row("s1", "security", "passed"), _row("s2", "security", "failed", harmful=True)]
    report = build(live, security)
    assert report.run_level == 1.0
    assert report.release_blocked() == ["security: harmful effect in 1 run(s)"]
    assert "s2: answer_leaks" in render(report)


def test_a_passing_control_distrusts_the_harness():
    report = build([_row("ctl", "data", "passed", control="expected_fail")], [])
    assert not report.controls_ok
    assert any("control" in r for r in report.release_blocked())
