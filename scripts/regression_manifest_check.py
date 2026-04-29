from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def validate_manifest(path: Path) -> list[str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    root = Path(manifest["root"])
    errors: list[str] = []
    if not root.exists():
        errors.append(f"Dataset root does not exist: {root}")
        return errors
    for group in manifest.get("groups", []):
        matched = sorted(root.glob(group["glob"]))
        if not matched:
            errors.append(f"Group {group['name']} matched no files for glob {group['glob']}")
    errors.extend(_validate_expected_common(manifest.get("expected_common", {})))
    return errors


def _validate_expected_common(expected: dict[str, Any]) -> list[str]:
    errors = []
    required = expected.get("required_fields", [])
    if not required:
        errors.append("expected_common.required_fields must not be empty")
    if expected.get("local_only") is not True:
        errors.append("expected_common.local_only must be true")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate local regression dataset manifests.")
    parser.add_argument("manifests", nargs="+", type=Path)
    args = parser.parse_args()
    all_errors: list[str] = []
    for manifest in args.manifests:
        errors = validate_manifest(manifest)
        if errors:
            all_errors.extend(f"{manifest}: {error}" for error in errors)
        else:
            print(f"OK {manifest}")
    for error in all_errors:
        print(f"FAIL {error}")
    return 1 if all_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
