"""Source-grouped, class-balanced CAD/BIM pipeline regression contract.

This module deliberately does not know how to open a sealed holdout. It only
aggregates explicit ``dev`` observations produced by domain evaluators. Every
source group gets equal weight inside its class and every class gets equal
weight in the macro score, so abundant easy variants cannot hide a rare-class
failure.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


StageName = Literal["reader", "graph", "model_3d_bim", "drawing_2d"]
StageStatus = Literal[
    "passed", "failed", "blocked", "review_required", "not_run", "not_applicable",
]
FailureCluster = Literal[
    "routing", "ocr_symbol", "view_association", "parameter", "feature",
    "topology", "bim_relation", "connectivity", "projection", "editor",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageObservation(StrictModel):
    status: StageStatus
    failure_clusters: list[FailureCluster] = Field(default_factory=list)
    blocker_codes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_pass_evidence(self) -> "StageObservation":
        if self.status == "passed" and not self.evidence_ids:
            raise ValueError("passed stage requires evidence_ids")
        if self.status == "not_applicable" and not self.blocker_codes:
            raise ValueError("not_applicable stage requires a reason code")
        if self.status == "not_applicable" and self.evidence_ids:
            raise ValueError("not_applicable stage cannot claim evidence")
        return self


class SafetyObservation(StrictModel):
    status: Literal["evaluated", "not_run"]
    invented_geometry: bool | None = None
    critical_miss: bool | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence(self) -> "SafetyObservation":
        if self.status == "evaluated" and (
            self.invented_geometry is None or self.critical_miss is None
        ):
            raise ValueError("evaluated safety requires both boolean outcomes")
        if self.status == "evaluated" and not self.evidence_ids:
            raise ValueError("evaluated safety requires evidence_ids")
        if self.status == "not_run" and (
            self.invented_geometry is not None
            or self.critical_miss is not None
            or self.evidence_ids
        ):
            raise ValueError("not_run safety cannot claim outcomes")
        return self


class PipelineCase(StrictModel):
    id: str
    source_group_id: str
    split: Literal["dev"]
    domain: Literal["mechanical", "construction", "assembly", "system"]
    drawing_class: str
    complexity: Literal["simple", "medium", "complex"]
    source_license: str
    truth_kind: str
    stages: dict[StageName, StageObservation]
    safety: SafetyObservation

    @model_validator(mode="after")
    def validate_stages(self) -> "PipelineCase":
        expected = {"reader", "graph", "model_3d_bim", "drawing_2d"}
        if set(self.stages) != expected:
            raise ValueError(f"pipeline case must define stages {sorted(expected)}")
        return self


class PipelineManifest(StrictModel):
    schema_version: Literal["cad-class-balanced/1.0"]
    dataset_id: str
    required_classes: list[str] = Field(min_length=1)
    min_source_groups_per_class: int = Field(default=1, ge=1)
    min_stage_pass_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    cases: list[PipelineCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_classes_and_groups(self) -> "PipelineManifest":
        if len(self.required_classes) != len(set(self.required_classes)):
            raise ValueError("required_classes must be unique")
        declared = set(self.required_classes)
        actual = {item.drawing_class for item in self.cases}
        if actual - declared:
            raise ValueError(f"undeclared drawing classes: {sorted(actual - declared)}")
        group_classes: dict[str, set[str]] = defaultdict(set)
        for item in self.cases:
            group_classes[item.source_group_id].add(item.drawing_class)
        mixed = {
            group: sorted(classes)
            for group, classes in group_classes.items() if len(classes) != 1
        }
        if mixed:
            raise ValueError(f"source groups span drawing classes: {mixed}")
        return self


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 8) if values else 0.0


def _aggregate_group(cases: list[PipelineCase]) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    failure_clusters: set[str] = set()
    blocker_codes: set[str] = set()
    for stage in ("reader", "graph", "model_3d_bim", "drawing_2d"):
        observations = [item.stages[stage] for item in cases]
        applicable = [
            item for item in observations if item.status != "not_applicable"
        ]
        for item in observations:
            failure_clusters.update(item.failure_clusters)
            blocker_codes.update(item.blocker_codes)
        stages[stage] = {
            "case_count": len(observations),
            "applicable_case_count": len(applicable),
            "applicable": bool(applicable),
            "coverage_rate": _mean([
                float(item.status != "not_run") for item in applicable
            ]) if applicable else None,
            "pass_rate": _mean([
                float(item.status == "passed") for item in applicable
            ]) if applicable else None,
            "status_counts": {
                status: sum(item.status == status for item in observations)
                for status in (
                    "passed", "failed", "blocked", "review_required", "not_run",
                    "not_applicable",
                )
                if any(item.status == status for item in observations)
            },
            "evidence_ids": sorted({
                evidence_id
                for item in observations
                for evidence_id in item.evidence_ids
            }),
        }
    safety = [item.safety for item in cases]
    evaluated = [item for item in safety if item.status == "evaluated"]
    return {
        "case_count": len(cases),
        "stages": stages,
        "safety_coverage_rate": _mean([
            float(item.status == "evaluated") for item in safety
        ]),
        "invented_geometry_rate": (
            _mean([float(bool(item.invented_geometry)) for item in evaluated])
            if evaluated else None
        ),
        "critical_miss_rate": (
            _mean([float(bool(item.critical_miss)) for item in evaluated])
            if evaluated else None
        ),
        "safety_evidence_ids": sorted({
            evidence_id for item in evaluated for evidence_id in item.evidence_ids
        }),
        "failure_clusters": sorted(failure_clusters),
        "blocker_codes": sorted(blocker_codes),
    }


def _regression_report(
    current: dict[str, Any], baseline: dict[str, Any] | None
) -> dict[str, Any]:
    if baseline is None:
        return {"status": "not_checked", "regressions": []}
    if baseline.get("schema_version") != "cad-class-balanced-report/1.0":
        raise ValueError("unsupported class-balanced baseline schema")
    regressions: list[dict[str, Any]] = []
    current_classes = current["by_class"]
    baseline_classes = baseline.get("by_class", {})
    for class_name, before in sorted(baseline_classes.items()):
        after = current_classes.get(class_name)
        if after is None:
            regressions.append({"code": "class_missing", "class": class_name})
            continue
        for stage, before_stage in before["stages"].items():
            after_stage = after["stages"][stage]
            if not after_stage.get("applicable", True):
                if before_stage.get("pass_rate") not in (None, 0, 0.0):
                    regressions.append({
                        "code": "stage_became_not_applicable",
                        "class": class_name,
                        "stage": stage,
                    })
                continue
            for metric in ("coverage_rate", "pass_rate"):
                before_value = before_stage.get(metric)
                after_value = after_stage.get(metric)
                if before_value is not None and (
                    after_value is None or after_value < before_value
                ):
                    regressions.append({
                        "code": f"stage_{metric}_regression",
                        "class": class_name,
                        "stage": stage,
                        "before": before_stage[metric],
                        "after": after_stage[metric],
                    })
        for metric in ("invented_geometry_rate", "critical_miss_rate"):
            before_value = before.get(metric)
            after_value = after.get(metric)
            if before_value is not None and (
                after_value is None or after_value > before_value
            ):
                regressions.append({
                    "code": f"{metric}_regression",
                    "class": class_name,
                    "before": before_value,
                    "after": after_value,
                })
    return {
        "status": "passed" if not regressions else "failed",
        "regressions": regressions,
    }


def _validate_safety_evidence(
    manifest: PipelineManifest, safety_report: dict[str, Any] | None
) -> None:
    if safety_report is None:
        return
    if safety_report.get("schema_version") != "emg-admission-corruption-report/1.0":
        raise ValueError("unsupported safety evidence report schema")
    scenarios = {
        item.get("id"): item for item in safety_report.get("scenarios", [])
        if isinstance(item, dict) and item.get("id")
    }
    for case in manifest.cases:
        if case.safety.status != "evaluated":
            continue
        evidence = []
        for evidence_id in case.safety.evidence_ids:
            scenario = scenarios.get(evidence_id)
            if scenario is None:
                raise ValueError(
                    f"{case.id}: safety evidence not found: {evidence_id}"
                )
            if scenario.get("source_group_id") != case.source_group_id:
                raise ValueError(
                    f"{case.id}: safety evidence source group mismatch: {evidence_id}"
                )
            if scenario.get("drawing_class") != case.drawing_class:
                raise ValueError(
                    f"{case.id}: safety evidence class mismatch: {evidence_id}"
                )
            evidence.append(scenario)
        actual_critical_miss = any(bool(item.get("critical_miss")) for item in evidence)
        actual_invented = any(bool(item.get("generator_allowed")) for item in evidence)
        if case.safety.critical_miss != actual_critical_miss:
            raise ValueError(f"{case.id}: critical_miss disagrees with safety evidence")
        if case.safety.invented_geometry != actual_invented:
            raise ValueError(
                f"{case.id}: invented_geometry disagrees with safety evidence"
            )


def _stage_evidence_index(
    evidence_reports: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for report in evidence_reports:
        schema = report.get("schema_version")
        if schema == "emg-regression-report/1.0":
            graph_entries = [
                {
                    "id": f"emg:{item['id']}",
                    "case_id": item["id"],
                    "stage": "graph",
                    "passed": item.get("passed") is True,
                }
                for item in report.get("cases", [])
            ]
            reader_entries = [
                {
                    "id": f"reader:{item['id']}",
                    "case_id": item["id"],
                    "stage": "reader",
                    "passed": item.get("passed") is True,
                }
                for item in report.get("cases", [])
            ]
            entries = graph_entries + reader_entries
        elif schema == "emg-artifact-regression-report/1.0":
            entries = report.get("cases", [])
        elif schema == "emg-live-stage-report/1.0":
            entries = report.get("evidence", [])
        elif schema == "emg-domain-build-report/1.0":
            entries = report.get("evidence", [])
        else:
            raise ValueError(f"unsupported stage evidence report schema: {schema}")
        for item in entries:
            evidence_id = item.get("id")
            if not evidence_id or evidence_id in evidence:
                raise ValueError(f"duplicate or missing stage evidence id: {evidence_id}")
            evidence[evidence_id] = item
    return evidence


def _validate_stage_evidence(
    manifest: PipelineManifest, evidence_reports: list[dict[str, Any]] | None
) -> None:
    if evidence_reports is None:
        return
    evidence = _stage_evidence_index(evidence_reports)
    for case in manifest.cases:
        for stage, observation in case.stages.items():
            if observation.status != "passed":
                continue
            for evidence_id in observation.evidence_ids:
                item = evidence.get(evidence_id)
                if item is None:
                    raise ValueError(
                        f"{case.id}: stage evidence not found: {evidence_id}"
                    )
                if item.get("case_id") != case.id or item.get("stage") != stage:
                    raise ValueError(
                        f"{case.id}: stage evidence target mismatch: {evidence_id}"
                    )
                if item.get("passed") is not True:
                    raise ValueError(
                        f"{case.id}: stage evidence did not pass: {evidence_id}"
                    )


def evaluate_class_balanced_manifest(
    payload: dict[str, Any], *, baseline: dict[str, Any] | None = None,
    safety_report: dict[str, Any] | None = None,
    evidence_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    manifest = PipelineManifest.model_validate(payload)
    _validate_safety_evidence(manifest, safety_report)
    _validate_stage_evidence(manifest, evidence_reports)
    by_group_cases: dict[str, list[PipelineCase]] = defaultdict(list)
    for item in manifest.cases:
        by_group_cases[item.source_group_id].append(item)
    groups = {
        group: _aggregate_group(cases)
        for group, cases in sorted(by_group_cases.items())
    }
    class_groups: dict[str, list[str]] = defaultdict(list)
    group_class = {
        group: cases[0].drawing_class for group, cases in by_group_cases.items()
    }
    for group, class_name in group_class.items():
        class_groups[class_name].append(group)

    by_class: dict[str, Any] = {}
    promotion_failures: list[dict[str, Any]] = []
    if evidence_reports is None:
        promotion_failures.append({"code": "stage_evidence_not_validated"})
    if safety_report is None:
        promotion_failures.append({"code": "safety_evidence_not_validated"})
    for class_name in manifest.required_classes:
        group_ids = sorted(class_groups.get(class_name, []))
        class_result: dict[str, Any] = {
            "source_groups": len(group_ids),
            "stages": {},
        }
        if len(group_ids) < manifest.min_source_groups_per_class:
            promotion_failures.append({
                "code": "insufficient_source_groups",
                "class": class_name,
                "required": manifest.min_source_groups_per_class,
                "actual": len(group_ids),
            })
        for stage in ("reader", "graph", "model_3d_bim", "drawing_2d"):
            applicable_groups = [
                groups[group] for group in group_ids
                if groups[group]["stages"][stage]["applicable"]
            ]
            applicable = bool(applicable_groups)
            coverage = _mean([
                group["stages"][stage]["coverage_rate"]
                for group in applicable_groups
            ]) if applicable else None
            passed = _mean([
                group["stages"][stage]["pass_rate"]
                for group in applicable_groups
            ]) if applicable else None
            class_result["stages"][stage] = {
                "applicable": applicable,
                "coverage_rate": coverage,
                "pass_rate": passed,
            }
            if applicable and coverage < 1.0:
                promotion_failures.append({
                    "code": "stage_coverage_incomplete",
                    "class": class_name,
                    "stage": stage,
                    "actual": coverage,
                })
            if applicable and passed < manifest.min_stage_pass_rate:
                promotion_failures.append({
                    "code": "stage_pass_rate_below_gate",
                    "class": class_name,
                    "stage": stage,
                    "required": manifest.min_stage_pass_rate,
                    "actual": passed,
                })
        safety_coverage = _mean([groups[group]["safety_coverage_rate"] for group in group_ids])
        invented_values = [
            groups[group]["invented_geometry_rate"] for group in group_ids
            if groups[group]["invented_geometry_rate"] is not None
        ]
        critical_values = [
            groups[group]["critical_miss_rate"] for group in group_ids
            if groups[group]["critical_miss_rate"] is not None
        ]
        class_result["safety_coverage_rate"] = safety_coverage
        class_result["invented_geometry_rate"] = (
            _mean(invented_values) if invented_values else None
        )
        class_result["critical_miss_rate"] = (
            _mean(critical_values) if critical_values else None
        )
        class_result["failure_clusters"] = sorted({
            cluster for group in group_ids for cluster in groups[group]["failure_clusters"]
        })
        if safety_coverage < 1.0:
            promotion_failures.append({
                "code": "safety_coverage_incomplete",
                "class": class_name,
                "actual": safety_coverage,
            })
        for metric in ("invented_geometry_rate", "critical_miss_rate"):
            value = class_result[metric]
            if value is not None and value > 0:
                promotion_failures.append({
                    "code": metric.removesuffix("_rate") + "_detected",
                    "class": class_name,
                    "actual": value,
                })
        by_class[class_name] = class_result

    macro = {
        "stages": {
            stage: {
                "applicable_class_count": sum(
                    item["stages"][stage]["applicable"]
                    for item in by_class.values()
                ),
                "coverage_rate": _mean([
                    item["stages"][stage]["coverage_rate"]
                    for item in by_class.values()
                    if item["stages"][stage]["applicable"]
                ]),
                "pass_rate": _mean([
                    item["stages"][stage]["pass_rate"]
                    for item in by_class.values()
                    if item["stages"][stage]["applicable"]
                ]),
            }
            for stage in ("reader", "graph", "model_3d_bim", "drawing_2d")
        },
    }
    report: dict[str, Any] = {
        "schema_version": "cad-class-balanced-report/1.0",
        "dataset_id": manifest.dataset_id,
        "split": "dev",
        "case_count": len(manifest.cases),
        "source_group_count": len(groups),
        "class_count": len(by_class),
        "weighting": "equal_source_group_within_class_then_equal_class_macro",
        "stage_evidence_validated": evidence_reports is not None,
        "safety_evidence_validated": safety_report is not None,
        "by_source_group": groups,
        "by_class": by_class,
        "macro": macro,
        "promotion_eligible": not promotion_failures,
        "promotion_failures": promotion_failures,
    }
    report["regression"] = _regression_report(report, baseline)
    if report["regression"]["status"] == "failed":
        report["promotion_failures"].append({"code": "regression_failed"})
        report["promotion_eligible"] = False
    report["accepted"] = report["regression"]["status"] != "failed"
    return report
