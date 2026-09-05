#!/usr/bin/env python3
"""Evaluate semantic PMI records and their evidence-backed associations."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import unicodedata
from collections.abc import Iterable
from typing import Any

SUPPORTED_SCHEMA = "nist-pmi-truth/1.0"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: PMI record must be an object")
        records.append(value)
    return records


def validate_reference(records: list[dict[str, Any]]) -> None:
    if not records:
        raise ValueError("PMI reference truth is empty")
    required = {
        "schema_version",
        "semantic_id",
        "suite",
        "primary_case_id",
        "category",
        "description",
        "specification",
        "evidence",
        "assurance",
    }
    seen = set()
    for index, record in enumerate(records, start=1):
        missing = sorted(required.difference(record))
        if missing:
            raise ValueError(f"reference record {index} missing: {', '.join(missing)}")
        if record["schema_version"] != SUPPORTED_SCHEMA:
            raise ValueError(f"reference record {index} has unsupported schema")
        semantic_id = record["semantic_id"]
        if semantic_id in seen:
            raise ValueError(f"duplicate reference semantic_id: {semantic_id}")
        seen.add(semantic_id)
        assurance = record["assurance"]
        evidence = record["evidence"]
        if assurance.get("geometry_linked") and not evidence.get("topology_targets"):
            raise ValueError(f"{semantic_id} has geometry_linked without topology_targets")
        if assurance.get("drawing_located") and not evidence.get("drawing_regions"):
            raise ValueError(f"{semantic_id} has drawing_located without drawing_regions")


def validate_candidate(records: list[dict[str, Any]]) -> None:
    seen = set()
    for index, record in enumerate(records, start=1):
        for field in ("category", "specification"):
            if field not in record:
                raise ValueError(f"candidate record {index} missing: {field}")
        semantic_id = record.get("semantic_id")
        if semantic_id:
            if semantic_id in seen:
                raise ValueError(f"duplicate candidate semantic_id: {semantic_id}")
            seen.add(semantic_id)


def _semantic_key(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        normalize_text(record.get("suite")),
        normalize_text(record.get("primary_case_id")),
        normalize_text(record.get("category")),
        normalize_text(record.get("specification")),
    )


def _counter_intersection_size(left: collections.Counter, right: collections.Counter) -> int:
    return sum((left & right).values())


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _targets(record: dict[str, Any], name: str) -> set[str]:
    values: Iterable[Any] = record.get("evidence", {}).get(name, [])
    return {normalize_text(value) for value in values if normalize_text(value)}


def _association_metrics(
    references: list[dict[str, Any]],
    candidates_by_id: dict[str, dict[str, Any]],
    assurance_flag: str,
    evidence_field: str,
) -> dict[str, Any]:
    linked = [record for record in references if record["assurance"].get(assurance_flag)]
    expected = sum(len(_targets(record, evidence_field)) for record in linked)
    predicted = 0
    correct = 0
    for reference in linked:
        candidate = candidates_by_id.get(reference["semantic_id"], {})
        actual_targets = _targets(candidate, evidence_field)
        expected_targets = _targets(reference, evidence_field)
        predicted += len(actual_targets)
        correct += len(actual_targets & expected_targets)
    precision = _ratio(correct, predicted)
    recall = _ratio(correct, expected)
    return {
        "eligible_reference_records": len(linked),
        "expected_targets": expected,
        "predicted_targets": predicted,
        "correct_targets": correct,
        "precision": precision if predicted else None,
        "recall": recall if expected else None,
        "f1": _f1(precision, recall) if predicted and expected else None,
    }


def evaluate(reference: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    validate_reference(reference)
    validate_candidate(candidate)
    reference_counter = collections.Counter(_semantic_key(record) for record in reference)
    candidate_counter = collections.Counter(_semantic_key(record) for record in candidate)
    correct = _counter_intersection_size(reference_counter, candidate_counter)
    precision = _ratio(correct, len(candidate))
    recall = _ratio(correct, len(reference))

    reference_by_id = {record["semantic_id"]: record for record in reference}
    candidate_by_id = {
        record["semantic_id"]: record for record in candidate if record.get("semantic_id")
    }
    common_ids = sorted(reference_by_id.keys() & candidate_by_id.keys())
    exact_spelling = sum(
        reference_by_id[item]["specification"] == candidate_by_id[item]["specification"]
        for item in common_ids
    )
    geometry = _association_metrics(
        reference, candidate_by_id, "geometry_linked", "topology_targets"
    )
    drawing = _association_metrics(reference, candidate_by_id, "drawing_located", "drawing_regions")

    verified_claims = [
        record
        for record in candidate
        if record.get("assurance", {}).get("semantic_status") == "verified"
    ]
    correct_keys = reference_counter & candidate_counter
    false_verified = sum(
        1
        for record in verified_claims
        if correct_keys[_semantic_key(record)] <= 0
        or not record.get("assurance", {}).get("geometry_linked")
        or not record.get("assurance", {}).get("drawing_located")
    )

    reference_geometry_complete = all(
        record["assurance"].get("geometry_linked") for record in reference
    )
    reference_drawing_complete = all(
        record["assurance"].get("drawing_located") for record in reference
    )
    failures = []
    if precision < 0.98:
        failures.append("semantic_precision_below_gate")
    if recall < 0.98:
        failures.append("semantic_recall_below_gate")
    if not reference_geometry_complete:
        failures.append("reference_geometry_links_incomplete")
    if not reference_drawing_complete:
        failures.append("reference_drawing_locations_incomplete")
    if false_verified:
        failures.append("false_verified_claims")

    return {
        "reference_records": len(reference),
        "candidate_records": len(candidate),
        "correct_semantic_records": correct,
        "semantic_precision": precision,
        "semantic_recall": recall,
        "semantic_f1": _f1(precision, recall),
        "missing_pmi_rate": _ratio(len(reference) - correct, len(reference)),
        "invented_pmi_rate": _ratio(len(candidate) - correct, len(candidate)),
        "common_semantic_ids": len(common_ids),
        "exact_source_spelling_accuracy": _ratio(exact_spelling, len(common_ids)),
        "geometry_attachment": geometry,
        "drawing_location": drawing,
        "verified_claims": len(verified_claims),
        "false_verified_claims": false_verified,
        "false_exact_rate": _ratio(false_verified, len(verified_claims)),
        "reference_promotion_ready": reference_geometry_complete and reference_drawing_complete,
        "promotion_eligible": not failures,
        "promotion_failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=pathlib.Path, required=True)
    parser.add_argument("--candidate", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--require-promotion", action="store_true")
    args = parser.parse_args()
    report = evaluate(_load_jsonl(args.reference), _load_jsonl(args.candidate))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["promotion_eligible"] or not args.require_promotion else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"PMI evaluation failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
