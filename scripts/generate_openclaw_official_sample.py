from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_openclaw_strict_gateway import build_strict_gateway  # noqa: E402


def build_official_sample() -> dict:
    strict_gateway = build_strict_gateway()
    return {
        "agents": {
            "defaults": {
                "skills": strict_gateway["skills"]["exposed"],
                "imageMaxDimensionPx": 1200,
                "model": {
                    "primary": "openai/gpt-5.5",
                    "fallbacks": [],
                },
                "models": {
                    "openai/gpt-5.5": {
                        "alias": "gpt-5.5",
                    }
                },
            }
        },
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "auth": {
                "mode": "token",
                "token": "${OPENCLAW_GATEWAY_TOKEN}",
            },
            "reload": {
                "mode": "hybrid",
                "debounceMs": 300,
            },
            "controlUi": {
                "allowedOrigins": [
                    "http://localhost:18789",
                    "http://127.0.0.1:18789",
                ]
            },
        },
        "session": {
            "dmScope": "per-channel-peer",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate official OpenClaw JSON sample.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("openclaw/config/openclaw.official.sample.json"),
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_official_sample(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
