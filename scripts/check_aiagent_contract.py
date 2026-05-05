from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATEWAY = ROOT / "aiagent" / "config" / "gateway.yml"
DEFAULT_REGISTRY = ROOT / "aiagent" / "skills" / "_registry.yml"


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _registry_tools(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(tool.get("name")): tool
        for tool in registry.get("tools", [])
        if tool.get("name")
    }


def check_contract(
    gateway_path: Path = DEFAULT_GATEWAY,
    registry_path: Path = DEFAULT_REGISTRY,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    gateway = _load_yaml(gateway_path)
    registry = _load_yaml(registry_path)
    tools = _registry_tools(registry)
    tool_names = set(tools)

    skills_cfg = gateway.get("skills") or {}
    exposed = set(skills_cfg.get("exposed") or [])
    approval_gates = set(skills_cfg.get("approval_gates") or [])
    approval_required = {
        name
        for name, tool in tools.items()
        if bool(tool.get("approval_required"))
    }

    errors: list[str] = []
    warnings: list[str] = []

    exposed_missing = sorted(exposed - tool_names)
    if exposed_missing:
        errors.append(f"gateway exposes unknown tools: {exposed_missing}")

    gates_missing = sorted(approval_gates - tool_names)
    if gates_missing:
        errors.append(f"approval gates reference unknown tools: {gates_missing}")

    gates_not_exposed = sorted(approval_gates - exposed)
    if gates_not_exposed:
        errors.append(f"approval gates are not exposed: {gates_not_exposed}")

    missing_required_gates = sorted(approval_required - approval_gates)
    if missing_required_gates:
        errors.append(f"approval_required tools missing gates: {missing_required_gates}")

    excessive_gates = sorted(approval_gates - approval_required)
    if excessive_gates:
        warnings.append(f"manual extra approval gates: {excessive_gates}")

    registry_not_exposed = sorted(tool_names - exposed)
    if registry_not_exposed:
        message = f"implemented tools not exposed: {registry_not_exposed}"
        if strict:
            errors.append(message)
        else:
            warnings.append(message)

    return {
        "ok": not errors,
        "strict": strict,
        "registry_tools": len(tool_names),
        "exposed_tools": len(exposed),
        "approval_gates": len(approval_gates),
        "errors": errors,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check AiAgent registry/gateway contract.")
    parser.add_argument("--gateway", type=Path, default=DEFAULT_GATEWAY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = check_contract(args.gateway, args.registry, strict=args.strict)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(
            "OK aiagent contract: "
            f"{result['registry_tools']} registry tools, "
            f"{result['exposed_tools']} exposed, "
            f"{result['approval_gates']} approval gates"
        )
        for warning in result["warnings"]:
            print(f"WARN {warning}")
    else:
        for error in result["errors"]:
            print(f"ERROR {error}")
        for warning in result["warnings"]:
            print(f"WARN {warning}")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
