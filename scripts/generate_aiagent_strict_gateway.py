from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATEWAY = ROOT / "aiagent" / "config" / "gateway.yml"
DEFAULT_REGISTRY = ROOT / "aiagent" / "skills" / "_registry.yml"
DEFAULT_OUTPUT = ROOT / "aiagent" / "config" / "gateway.strict.yml"


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_strict_gateway(
    gateway_path: Path = DEFAULT_GATEWAY,
    registry_path: Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    gateway = _load_yaml(gateway_path)
    registry = _load_yaml(registry_path)
    tools = sorted(
        (
            tool
            for tool in registry.get("tools", [])
            if tool.get("name")
        ),
        key=lambda tool: str(tool["name"]),
    )
    tool_names = {str(tool["name"]) for tool in tools}

    strict = dict(gateway)
    strict["skills"] = {
        **(gateway.get("skills") or {}),
        "registry": "./skills/_registry.yml",
        "exposed": [str(tool["name"]) for tool in tools],
        "approval_gates": [
            str(tool["name"])
            for tool in tools
            if bool(tool.get("approval_required"))
        ],
    }
    strict["scenarios"] = [
        scenario
        for scenario in gateway.get("scenarios", [])
        if _scenario_is_supported(scenario, tool_names)
    ]
    return strict


def _scenario_is_supported(scenario: dict[str, Any], tool_names: set[str]) -> bool:
    path_value = scenario.get("path")
    if not path_value:
        return True
    scenario_path = (ROOT / "aiagent" / str(path_value).lstrip("./")).resolve()
    if not scenario_path.exists():
        return False
    raw = _load_yaml(scenario_path)
    referenced = {
        str(step.get("skill"))
        for step in raw.get("steps", [])
        if isinstance(step, dict) and step.get("skill")
    }
    return referenced.issubset(tool_names)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate strict AiAgent gateway config.")
    parser.add_argument("--gateway", type=Path, default=DEFAULT_GATEWAY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    strict = build_strict_gateway(args.gateway, args.registry)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(strict, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"Generated {args.output}")


if __name__ == "__main__":
    main()
