"""Exact assembly and construction build evidence for the EMG dev manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.ai.construction_emg import ConstructionModel, compile_construction_ifc
from scripts.live_emg_stack_regression import _post, _zip_members


def _assembly_result(case: dict[str, Any]) -> dict[str, Any]:
    payload = case["input"]
    kernel_payload = {
        "name": payload["name"],
        "components": payload["drawing_components"],
        "metadata": {"emg_case_id": case["id"]},
    }
    status, body, _ = _post("/assembly/compile", kernel_payload)
    if status != 200:
        raise RuntimeError(f"assembly compile HTTP {status}: {body[:400]!r}")
    repeated_status, repeated_body, _ = _post("/assembly/compile", kernel_payload)
    if repeated_status != 200:
        raise RuntimeError(
            f"repeated assembly compile HTTP {repeated_status}: {repeated_body[:400]!r}"
        )
    members = _zip_members(body)
    repeated = _zip_members(repeated_body)
    report = json.loads(members["assembly-report.json"])
    reopen = report.get("reopen") or {}
    step = members["assembly.step"]
    checks = {
        "brep_valid": report.get("assembly", {}).get("brep_valid") is True,
        "component_count": len(report.get("components", [])) == len(payload["drawing_components"]),
        "step_signature": step.startswith(b"ISO-10303-21"),
        "step_reopen_valid": reopen.get("valid") is True,
        "step_reopen_sha_matches": reopen.get("step_sha256") == hashlib.sha256(step).hexdigest(),
        "artifact_deterministic": step == repeated["assembly.step"],
    }
    return {
        "id": f"build:{case['id']}:model_3d_bim",
        "case_id": case["id"],
        "stage": "model_3d_bim",
        "passed": all(checks.values()),
        "checks": checks,
        "artifact_sha256": hashlib.sha256(step).hexdigest(),
        "reopen": reopen,
    }


def _construction_result(case: dict[str, Any]) -> dict[str, Any]:
    model = ConstructionModel.model_validate(case["input"]["model"])
    artifact, report = compile_construction_ifc(model)
    repeated_artifact, repeated_report = compile_construction_ifc(model)
    checks = {
        "ifc_signature": artifact.startswith(b"ISO-10303-21"),
        "ifc_reopen_valid": report.get("valid") is True,
        "schema_ifc4": report.get("schema") == "IFC4",
        "all_products_represented": report.get("represented_product_count") == len(model.elements),
        "geometry_reopened": not report.get("geometry_failures"),
        "artifact_sha_matches": report.get("ifc_sha256") == hashlib.sha256(artifact).hexdigest(),
        "artifact_deterministic": artifact == repeated_artifact
        and report.get("ifc_sha256") == repeated_report.get("ifc_sha256"),
    }
    return {
        "id": f"build:{case['id']}:model_3d_bim",
        "case_id": case["id"],
        "stage": "model_3d_bim",
        "passed": all(checks.values()),
        "checks": checks,
        "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        "product_class_counts": report.get("product_class_counts"),
    }


def run_domain_build_regression(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != "emg-regression/1.0":
        raise ValueError("unsupported EMG regression manifest schema")
    by_kind = {item["kind"]: item for item in manifest.get("cases", [])}
    missing = {"assembly", "construction"} - set(by_kind)
    if missing:
        raise ValueError(f"missing exact domain build cases: {sorted(missing)}")
    evidence = [
        _assembly_result(by_kind["assembly"]),
        _construction_result(by_kind["construction"]),
    ]
    return {
        "schema_version": "emg-domain-build-report/1.0",
        "split": "dev",
        "passed": all(item["passed"] for item in evidence),
        "evidence": evidence,
    }


def load_and_run(manifest_path: Path) -> dict[str, Any]:
    return run_domain_build_regression(json.loads(manifest_path.read_text()))
