#!/usr/bin/env python3
"""Validate universal CAD/BIM source manifest v2 without weakening truth claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from collections import Counter, defaultdict
from typing import Any

REQUIRED_TRUTH = {
    "dxf": {"vector_drawing_geometry"},
    "step": {"brep_geometry"},
    "ifc": {"ifc_semantics", "ifc_geometry", "spatial_relations"},
}
DOMAINS = {"mechanical", "construction", "mixed"}
SPLITS = {"train", "val", "holdout"}


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_rows(rows: list[dict[str, Any]], *, verify_files: bool = True) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    group_splits: dict[str, set[str]] = defaultdict(set)
    identities: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        def issue(code: str, detail: str) -> None:
            issues.append({"row": index, "code": code, "detail": detail})

        required = (
            "schema_version", "source_id", "source_group_id", "domain",
            "drawing_class", "asset_format", "truth_layers", "license",
            "sha256", "split", "output_path",
        )
        missing = [key for key in required if row.get(key) in (None, "", [])]
        if missing:
            issue("missing_fields", ",".join(missing))
            continue
        if row["schema_version"] != 2:
            issue("schema_version", str(row["schema_version"]))
        if row["domain"] not in DOMAINS:
            issue("domain", str(row["domain"]))
        if row.get("profile") != row["domain"]:
            issue("legacy_profile_mismatch", f"{row.get('profile')} != {row['domain']}")
        if row["split"] not in SPLITS:
            issue("split", str(row["split"]))
        asset_format = str(row["asset_format"]).lower()
        expected_truth = REQUIRED_TRUTH.get(asset_format)
        if expected_truth is None:
            issue("asset_format", asset_format)
        elif not expected_truth.issubset(set(row["truth_layers"])):
            issue("truth_layers", f"{asset_format} requires {sorted(expected_truth)}")
        identity = (row["source_id"], row["relative_path"])
        if identity in identities:
            issue("duplicate_source_asset", ":".join(identity))
        identities.add(identity)
        group_splits[row["source_group_id"]].add(row["split"])
        if verify_files:
            path = pathlib.Path(row["output_path"])
            if not path.is_file():
                issue("missing_file", str(path))
            elif _sha256(path) != row["sha256"]:
                issue("sha256_mismatch", str(path))

    for group, splits in sorted(group_splits.items()):
        if len(splits) > 1:
            issues.append({
                "row": None,
                "code": "source_group_leakage",
                "detail": f"{group}: {sorted(splits)}",
            })
    return {
        "schema_version": 2,
        "ok": not issues,
        "assets": len(rows),
        "source_groups": len(group_splits),
        "domains": dict(sorted(Counter(row.get("domain", "missing") for row in rows).items())),
        "formats": dict(sorted(Counter(row.get("asset_format", "missing") for row in rows).items())),
        "classes": dict(sorted(Counter(row.get("drawing_class", "missing") for row in rows).items())),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=pathlib.Path)
    parser.add_argument("--no-file-check", action="store_true")
    parser.add_argument("--report", type=pathlib.Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    report = validate_rows(rows, verify_files=not args.no_file_check)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(rendered)
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
