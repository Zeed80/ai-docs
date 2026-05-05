from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_aiagent_strict_gateway import build_strict_gateway  # noqa: E402

DEFAULT_OUTPUT = ROOT / "aiagent" / "config" / "aiagent.official.sample.json"


def build_official_sample() -> dict[str, Any]:
    strict_gateway = build_strict_gateway()
    gateway_agent = strict_gateway.get("agent") or {}
    return {
        "gateway": {
            "auth": {
                "mode": "token",
                "token": "${AIAGENT_GATEWAY_TOKEN}",
            },
            "bind": "0.0.0.0",
            "port": 18789,
        },
        "session": {
            "dmScope": "per-channel-peer",
        },
        "agents": {
            "defaults": {
                "name": gateway_agent.get("name", "Света"),
                "language": gateway_agent.get("language", "ru"),
                "skills": strict_gateway["skills"]["exposed"],
            }
        },
        "tools": {
            "registry": "aiagent/skills/_registry.yml",
            "approvalGates": strict_gateway["skills"]["approval_gates"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate official AiAgent sample config.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    sample = build_official_sample()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Generated {args.output}")


if __name__ == "__main__":
    main()
