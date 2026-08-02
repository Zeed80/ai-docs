#!/usr/bin/env python3
"""Project IFC manifest assets in isolated production-image processes."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--image", default="infra-backend")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    rows = [row for row in rows if row.get("asset_format") == "ifc"]
    script = (
        pathlib.Path(__file__).resolve().parents[2]
        / "backend" / "scripts" / "project_ifc_views.py"
    )
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    records = []
    for row in rows:
        source = pathlib.Path(row["output_path"]).resolve()
        target = out / f"{source.stem}.json"
        result = subprocess.run(
            [
                "docker", "run", "--rm", "--entrypoint", "python",
                "-v", f"{script}:/app/project_ifc_views.py:ro",
                "-v", f"{source.parent}:/data:ro",
                "-v", f"{out}:/out",
                args.image,
                "/app/project_ifc_views.py", f"/data/{source.name}",
                "--out", f"/out/{target.name}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        records.append({
            "source_group_id": row["source_group_id"],
            "drawing_class": row["drawing_class"],
            "source": str(source),
            "projection": str(target),
            "ok": result.returncode == 0 and target.exists(),
            "stdout": result.stdout.strip()[-1000:],
            "stderr": result.stderr.strip()[-1000:],
        })
    summary = {
        "schema_version": 1,
        "ifc_assets": len(records),
        "projected": sum(row["ok"] for row in records),
        "failed": sum(not row["ok"] for row in records),
        "records": records,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({key: summary[key] for key in ("ifc_assets", "projected", "failed")}, indent=2))
    return 0 if records and summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
