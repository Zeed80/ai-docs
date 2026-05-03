from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_aiagent_contract import _collect_scenario_skills


def build_strict_gateway(
    source_path: Path = Path("aiagent/config/gateway.yml"),
) -> dict[str, Any]:
    gateway = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    registry_path = _resolve_registry_path(source_path, gateway["skills"]["registry"])
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    registry_tools = {tool["name"]: tool for tool in registry["tools"]}

    strict_gateway = deepcopy(gateway)
    strict_gateway["skills"]["exposed"] = sorted(registry_tools)
    strict_gateway["skills"]["approval_gates"] = sorted(
        tool["name"] for tool in registry["tools"] if tool.get("approval_required")
    )
    strict_gateway["scenarios"] = [
        scenario
        for scenario in gateway.get("scenarios", [])
        if _scenario_is_supported(source_path.parent.parent, scenario, registry_tools)
    ]
    strict_gateway["metadata"] = {
        **strict_gateway.get("metadata", {}),
        "generated": True,
        "source": str(source_path),
        "policy": "strict_generated_tools_only",
    }
    return strict_gateway


def _resolve_registry_path(gateway_path: Path, registry_value: str) -> Path:
    raw = Path(registry_value)
    if raw.is_absolute():
        return raw
    return (gateway_path.parent.parent / raw).resolve()


def _scenario_is_supported(
    aiagent_root: Path,
    scenario: dict[str, Any],
    registry_tools: dict[str, Any],
) -> bool:
    skills = _collect_scenario_skills(aiagent_root, [scenario])
    return bool(skills) and all(skill in registry_tools for skill in skills)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate strict AiAgent Gateway config.")
    parser.add_argument("--source", type=Path, default=Path("aiagent/config/gateway.yml"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("aiagent/config/gateway.strict.yml"),
    )
    args = parser.parse_args()

    strict_gateway = build_strict_gateway(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(strict_gateway, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
