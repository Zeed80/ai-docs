from __future__ import annotations

from scripts.turboquant_benchmark import (
    RunResult,
    _load_quality_prompts,
    evaluate_case,
    match_expected_terms,
    summarize_quality,
)


def test_turboquant_quality_prompts_include_engineering_terms() -> None:
    prompts = _load_quality_prompts("docs/technology-regression-manifest.json")

    assert prompts
    assert "Сталь 40Х" in prompts[0]["expected_terms"]
    assert "ГОСТ 2789-73" in prompts[0]["expected_terms"]
    assert "010" in prompts[0]["expected_terms"]


def test_turboquant_term_matching_is_case_and_yo_insensitive() -> None:
    matched, missing = match_expected_terms(
        "Материал сталь 40х, контроль по гост 2789-73, все еще актуально.",
        ["Сталь 40Х", "ГОСТ 2789-73", "резец проходной"],
    )

    assert matched == ["Сталь 40Х", "ГОСТ 2789-73"]
    assert missing == ["резец проходной"]


def test_turboquant_quality_case_uses_best_successful_output() -> None:
    result = evaluate_case(
        [
            RunResult(ok=False, latency_ms=1, output_chars=0, error="boom"),
            RunResult(ok=True, latency_ms=2, output_chars=18, text="Сталь 40Х и 16К20"),
        ],
        ["Сталь 40Х", "16К20", "ГОСТ 2789-73"],
        threshold=0.6,
    )

    assert result["passed"] is True
    assert result["term_recall"] == 0.6667
    assert result["missing_terms"] == ["ГОСТ 2789-73"]


def test_turboquant_quality_summary_reports_each_side() -> None:
    summary = summarize_quality(
        [
            {
                "baseline": {"passed": True, "term_recall": 1.0},
                "turboquant": {"passed": False, "term_recall": 0.5},
            }
        ]
    )

    assert summary["baseline"]["pass_rate"] == 1.0
    assert summary["turboquant"]["pass_rate"] == 0.0
    assert summary["turboquant"]["term_recall_avg"] == 0.5
