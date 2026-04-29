from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.ai.evals.agent_roles import validate_agent_role_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate built-in agent role regression manifest.")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("docs/agent-role-regression-manifest.json"),
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path("openclaw/skills/_registry.yml"),
    )
    args = parser.parse_args()

    errors = validate_agent_role_manifest(args.manifest, args.registry)
    if errors:
        for error in errors:
            print(f"FAIL {error}")
        return 1
    print(f"OK {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
