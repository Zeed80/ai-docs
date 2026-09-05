#!/usr/bin/env python3
"""Freeze and audit a CAD promotion candidate, then seal holdout results.

The three commands are intentionally separate.  ``freeze`` and ``audit`` must
run before anybody opens aggregate holdout scores.  ``finalize`` refuses to
overwrite an existing receipt, making the repository receipt the one-shot
boundary for a frozen candidate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
from collections import defaultdict
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROMOTION_THRESHOLDS = {
    "entity_precision": 0.995,
    "entity_recall": 0.995,
    "exact_sheet_rate": 0.99,
    "false_exact_rate": 0.0,
}


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _absolute(path: pathlib.Path) -> pathlib.Path:
    return path if path.is_absolute() else ROOT / path


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def freeze_candidate(
    candidate_commit: str,
    artifacts: list[pathlib.Path],
) -> dict[str, Any]:
    commit = _git("rev-parse", f"{candidate_commit}^{{commit}}")
    tree = _git("show", "-s", "--format=%T", commit)
    dirty = bool(_git("status", "--porcelain"))
    artifacts = [_absolute(path) for path in artifacts]
    return {
        "schema_version": "cad-final-freeze/1.0",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "freeze_tool_commit": _git("rev-parse", "HEAD"),
        "worktree_dirty_at_freeze": dirty,
        "artifacts": [
            {
                "path": str(path.relative_to(ROOT)),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in artifacts
        ],
    }


def audit_leakage(
    mechanical_manifest: pathlib.Path,
    construction_manifest: pathlib.Path,
    source_manifest: pathlib.Path,
    source_registry: pathlib.Path,
    tuning_roots: list[pathlib.Path],
) -> dict[str, Any]:
    mechanical_manifest = _absolute(mechanical_manifest)
    construction_manifest = _absolute(construction_manifest)
    source_manifest = _absolute(source_manifest)
    source_registry = _absolute(source_registry)
    tuning_roots = [_absolute(path) for path in tuning_roots]
    selected = {
        "mechanical": _load_jsonl(mechanical_manifest),
        "construction": [
            row for row in _load_jsonl(construction_manifest) if row.get("split") == "holdout"
        ],
    }
    holdout_groups = {
        domain: {str(row["source_group_id"]) for row in rows} for domain, rows in selected.items()
    }
    issues: list[dict[str, Any]] = []
    registry = {row["id"]: row for row in _load_json(source_registry).get("sources", [])}
    for domain, rows in selected.items():
        for row in rows:
            source_id = str(row.get("source_id") or row["source_group_id"].split(":", 1)[0])
            source = registry.get(source_id)
            license_name = row.get("license") or (source or {}).get("license")
            if not source or not str(source.get("status", "")).startswith("approved"):
                issues.append(
                    {"code": "source_not_approved", "domain": domain, "source_id": source_id}
                )
            if not license_name:
                issues.append({"code": "license_missing", "domain": domain, "source_id": source_id})
    source_rows = _load_jsonl(source_manifest)
    group_splits: dict[str, set[str]] = defaultdict(set)
    for row in source_rows:
        group_splits[str(row["source_group_id"])].add(str(row["split"]))
    for group, splits in sorted(group_splits.items()):
        if len(splits) > 1:
            issues.append({"code": "source_group_cross_split", "group": group})

    selected_groups = set().union(*holdout_groups.values())
    non_holdout_groups = {
        str(row["source_group_id"]) for row in source_rows if row.get("split") != "holdout"
    }
    for group in sorted(selected_groups & non_holdout_groups):
        issues.append({"code": "selected_holdout_in_non_holdout_split", "group": group})

    searchable_suffixes = {".json", ".jsonl", ".md", ".py", ".txt", ".yaml", ".yml"}
    for root in tuning_roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in searchable_suffixes:
                continue
            text = path.read_text(errors="replace")
            for group in sorted(selected_groups):
                if group in text:
                    issues.append(
                        {
                            "code": "holdout_group_in_tuning_surface",
                            "group": group,
                            "path": str(path.relative_to(ROOT)),
                        }
                    )

    return {
        "schema_version": "cad-holdout-leakage-audit/1.0",
        "ok": not issues,
        "selected": {
            domain: {"rows": len(selected[domain]), "source_groups": len(groups)}
            for domain, groups in holdout_groups.items()
        },
        "source_manifest_groups": len(group_splits),
        "tuning_surfaces": [str(path.relative_to(ROOT)) for path in tuning_roots],
        "issues": issues,
    }


def _score_domain(report: dict[str, Any]) -> dict[str, Any]:
    summary = report["summary"]
    checks = {
        metric: (
            summary[metric] == threshold
            if metric == "false_exact_rate"
            else summary[metric] >= threshold
        )
        for metric, threshold in PROMOTION_THRESHOLDS.items()
    }
    return {
        "passed": all(checks.values()) and summary["errors"] == 0,
        "checks": checks,
        "summary": summary,
    }


def finalize(
    freeze_path: pathlib.Path,
    leakage_path: pathlib.Path,
    mechanical_path: pathlib.Path,
    construction_path: pathlib.Path,
    output_path: pathlib.Path,
) -> dict[str, Any]:
    freeze_path = _absolute(freeze_path)
    leakage_path = _absolute(leakage_path)
    mechanical_path = _absolute(mechanical_path)
    construction_path = _absolute(construction_path)
    output_path = _absolute(output_path)
    if output_path.exists():
        raise FileExistsError(f"sealed receipt already exists: {output_path}")
    freeze = _load_json(freeze_path)
    leakage = _load_json(leakage_path)
    mechanical = _load_json(mechanical_path)
    construction = _load_json(construction_path)
    domains = {
        "mechanical": _score_domain(mechanical),
        "construction": _score_domain(construction),
    }
    passed = leakage.get("ok") is True and all(item["passed"] for item in domains.values())
    failures = []
    if leakage.get("ok") is not True:
        failures.append("holdout_leakage_audit_failed")
    failures.extend(
        f"{domain}_promotion_gate_failed"
        for domain, result in domains.items()
        if not result["passed"]
    )
    return {
        "schema_version": "cad-final-acceptance/1.0",
        "candidate_commit": freeze["candidate_commit"],
        "candidate_tree": freeze["candidate_tree"],
        "frozen_artifacts": freeze["artifacts"],
        "leakage_audit": {
            "ok": leakage.get("ok") is True,
            "selected": leakage.get("selected", {}),
            "issues": leakage.get("issues", []),
        },
        "holdout_policy": "one-shot; no tuning or rerun from sealed results",
        "promotion_thresholds": PROMOTION_THRESHOLDS,
        "inputs": {
            name: {"path": str(path.relative_to(ROOT)), "sha256": _sha256(path)}
            for name, path in {
                "freeze": freeze_path,
                "leakage": leakage_path,
                "mechanical": mechanical_path,
                "construction": construction_path,
            }.items()
        },
        "domains": domains,
        "promotion_eligible": passed,
        "verdict": "accepted" if passed else "rejected",
        "failures": failures,
    }


def _write(path: pathlib.Path, payload: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with path.open(mode) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--candidate-commit", required=True)
    freeze.add_argument("--artifact", action="append", type=pathlib.Path, required=True)
    freeze.add_argument("--out", type=pathlib.Path, required=True)
    audit = subparsers.add_parser("audit")
    audit.add_argument("--mechanical-manifest", type=pathlib.Path, required=True)
    audit.add_argument("--construction-manifest", type=pathlib.Path, required=True)
    audit.add_argument("--source-manifest", type=pathlib.Path, required=True)
    audit.add_argument("--source-registry", type=pathlib.Path, required=True)
    audit.add_argument("--tuning-root", action="append", type=pathlib.Path, required=True)
    audit.add_argument("--out", type=pathlib.Path, required=True)
    final = subparsers.add_parser("finalize")
    final.add_argument("--freeze", type=pathlib.Path, required=True)
    final.add_argument("--leakage", type=pathlib.Path, required=True)
    final.add_argument("--mechanical", type=pathlib.Path, required=True)
    final.add_argument("--construction", type=pathlib.Path, required=True)
    final.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()

    if args.command == "freeze":
        payload = freeze_candidate(args.candidate_commit, args.artifact)
        _write(args.out, payload)
        ok = True
    elif args.command == "audit":
        payload = audit_leakage(
            args.mechanical_manifest,
            args.construction_manifest,
            args.source_manifest,
            args.source_registry,
            args.tuning_root,
        )
        _write(args.out, payload)
        ok = payload["ok"]
    else:
        payload = finalize(
            args.freeze,
            args.leakage,
            args.mechanical,
            args.construction,
            args.out,
        )
        _write(args.out, payload, exclusive=True)
        ok = payload["promotion_eligible"]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
