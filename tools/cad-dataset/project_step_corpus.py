#!/usr/bin/env python3
"""Run every STEP projection in an isolated container process."""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    parser.add_argument("--image", default="infra-cad-kernel")
    args = parser.parse_args()
    source = args.source.resolve()
    out = args.out.resolve()
    script = pathlib.Path(__file__).with_name("project_step_edges.py").resolve()
    out.mkdir(parents=True, exist_ok=True)
    projected = 0
    failures = []
    for path in sorted(source.glob("*.step")):
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0",
                "--entrypoint",
                "/opt/cad-kernel/bin/python",
                "-v",
                f"{script}:/app/project_step_edges.py:ro",
                "-v",
                f"{source}:/data:ro",
                "-v",
                f"{out}:/out",
                args.image,
                "/app/project_step_edges.py",
                "--file",
                f"/data/{path.name}",
                "--out",
                "/out",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        target = out / f"{path.stem}.json"
        if result.returncode == 0 and target.exists():
            projected += 1
        else:
            failures.append(
                {
                    "source": path.name,
                    "returncode": result.returncode,
                    "stderr": result.stderr[-500:],
                }
            )
    summary = {"projected": projected, "failed": len(failures), "failures": failures}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({"projected": projected, "failed": len(failures)}, indent=2))
    return 0 if projected else 1


if __name__ == "__main__":
    raise SystemExit(main())
