#!/usr/bin/env python3
"""Benchmark baseline vs TurboQuant OpenAI-compatible vLLM endpoints."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class RunResult:
    ok: bool
    latency_ms: int
    output_chars: int
    text: str = ""
    error: str | None = None


DEFAULT_PROMPT = (
    "Кратко проверь технологический процесс: материал Сталь 40Х, токарная операция, "
    "контроль шероховатости по ГОСТ 2789-73. Укажи риски и недостающие данные."
)


async def run_once(
    *,
    base_url: str,
    model: str,
    prompt: str,
    timeout_seconds: float,
) -> RunResult:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 512,
                },
            )
            response.raise_for_status()
            body = response.json()
        text = body.get("choices", [{}])[0].get("message", {}).get("content") or ""
        return RunResult(
            ok=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
            output_chars=len(text),
            text=text,
        )
    except Exception as exc:
        return RunResult(
            ok=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            output_chars=0,
            error=str(exc),
        )


async def run_suite(args: argparse.Namespace) -> dict[str, Any]:
    prompts = _load_quality_prompts(args.quality_manifest) if args.quality_manifest else []
    if args.case_limit:
        prompts = prompts[: args.case_limit]
    prompt = args.prompt or DEFAULT_PROMPT
    if not prompts:
        prompts = [{"id": "default", "prompt": prompt, "expected_terms": []}]
    baseline = []
    turboquant = []
    quality = []
    for item in prompts:
        baseline_case_results = []
        turboquant_case_results = []
        for _ in range(args.runs):
            baseline_result = await run_once(
                base_url=args.baseline_url,
                model=args.baseline_model,
                prompt=item["prompt"],
                timeout_seconds=args.timeout,
            )
            turboquant_result = await run_once(
                base_url=args.turboquant_url,
                model=args.turboquant_model,
                prompt=item["prompt"],
                timeout_seconds=args.timeout,
            )
            baseline.append(baseline_result)
            turboquant.append(turboquant_result)
            baseline_case_results.append(baseline_result)
            turboquant_case_results.append(turboquant_result)
        quality.append(
            {
                "case_id": item["id"],
                "expected_terms": item["expected_terms"],
                "baseline": evaluate_case(
                    baseline_case_results,
                    item["expected_terms"],
                    threshold=args.quality_threshold,
                ),
                "turboquant": evaluate_case(
                    turboquant_case_results,
                    item["expected_terms"],
                    threshold=args.quality_threshold,
                ),
            }
        )
    return {
        "runs": args.runs,
        "cases": len(prompts),
        "baseline": summarize(baseline),
        "turboquant": summarize(turboquant),
        "quality": summarize_quality(quality),
        "quality_cases": quality,
        "decision_hint": (
            "TurboQuant can be promoted only if success_rate stays 1.0, "
            "term recall is not worse than baseline, and manual review passes "
            "on engineering regression cases."
        ),
    }


def summarize(results: list[RunResult]) -> dict[str, Any]:
    successes = [item for item in results if item.ok]
    latencies = [item.latency_ms for item in successes]
    return {
        "success_rate": len(successes) / len(results) if results else 0,
        "latency_ms_avg": int(statistics.mean(latencies)) if latencies else None,
        "latency_ms_p95": percentile(latencies, 0.95) if latencies else None,
        "output_chars_avg": int(statistics.mean([item.output_chars for item in successes]))
        if successes
        else 0,
        "errors": [item.error for item in results if item.error],
    }


def evaluate_case(
    results: list[RunResult],
    expected_terms: list[str],
    *,
    threshold: float,
) -> dict[str, Any]:
    ok_results = [item for item in results if item.ok]
    best_text = max(ok_results, key=lambda item: item.output_chars).text if ok_results else ""
    matched, missing = match_expected_terms(best_text, expected_terms)
    recall = len(matched) / len(expected_terms) if expected_terms else 1.0
    return {
        "passed": recall >= threshold,
        "term_recall": round(recall, 4),
        "matched_terms": matched,
        "missing_terms": missing,
    }


def match_expected_terms(text: str, expected_terms: list[str]) -> tuple[list[str], list[str]]:
    normalized_text = normalize_for_match(text)
    matched = []
    missing = []
    for term in expected_terms:
        normalized_term = normalize_for_match(term)
        if normalized_term and normalized_term in normalized_text:
            matched.append(term)
        else:
            missing.append(term)
    return matched, missing


def summarize_quality(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "baseline": _summarize_quality_side(cases, "baseline"),
        "turboquant": _summarize_quality_side(cases, "turboquant"),
    }


def _summarize_quality_side(cases: list[dict[str, Any]], side: str) -> dict[str, Any]:
    values = [case[side]["term_recall"] for case in cases]
    passed = [case for case in cases if case[side]["passed"]]
    return {
        "pass_rate": len(passed) / len(cases) if cases else 0,
        "term_recall_avg": round(statistics.mean(values), 4) if values else 0,
    }


def normalize_for_match(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


def _load_quality_prompts(manifest_path: str) -> list[dict[str, Any]]:
    manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest_root = Path(manifest["root"])
    root = manifest_root if manifest_root.exists() else manifest_file.parent / manifest_root
    prompts: list[dict[str, Any]] = []
    for case_file in sorted(root.glob("*.json")):
        case = json.loads(case_file.read_text(encoding="utf-8"))
        expected_terms = []
        for values in case.get("mentions", {}).values():
            expected_terms.extend(values)
        expected_terms.extend(str(item) for item in case.get("expected_operations", []))
        if case.get("expected_operation_type"):
            expected_terms.append(str(case["expected_operation_type"]))
        prompt = (
            "Проанализируй синтетический инженерно-технологический кейс. "
            "Верни краткий вывод, какие материал, оборудование, инструмент, НТД и операции нужно учитывать. "
            "Сохраняй точные обозначения из кейса, если они важны для проверки.\n\n"
            + json.dumps(case, ensure_ascii=False, indent=2)
        )
        prompts.append(
            {
                "id": case.get("case_id", case_file.stem),
                "prompt": prompt,
                "expected_terms": expected_terms,
            }
        )
    return prompts


def percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))
    return ordered[index]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare baseline vLLM and TurboQuant vLLM endpoints.")
    parser.add_argument("--baseline-url", default="http://localhost:8000")
    parser.add_argument("--turboquant-url", default="http://localhost:8001")
    parser.add_argument("--baseline-model", required=True)
    parser.add_argument("--turboquant-model", required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--quality-manifest", default=None)
    parser.add_argument("--quality-threshold", type=float, default=0.6)
    parser.add_argument("--case-limit", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(run_suite(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
