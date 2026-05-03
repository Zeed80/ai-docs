from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def check_contract(
    gateway_path: Path = Path("aiagent/config/gateway.yml"),
    *,
    strict: bool = False,
) -> dict[str, Any]:
    gateway = yaml.safe_load(gateway_path.read_text(encoding="utf-8"))
    registry_path = _resolve_registry_path(gateway_path, gateway["skills"]["registry"])
    registry = _load_registry(registry_path)
    registry_tools = {tool["name"]: tool for tool in registry["tools"]}
    exposed = gateway["skills"].get("exposed", [])
    approval_gates = gateway["skills"].get("approval_gates", [])

    errors: list[str] = []
    warnings: list[str] = []

    _check_duplicates("skills.exposed", exposed, errors)
    _check_duplicates("skills.approval_gates", approval_gates, errors)

    exposed_known = [skill for skill in exposed if skill in registry_tools]
    exposed_missing = sorted({skill for skill in exposed if skill not in registry_tools})
    if exposed_missing:
        warnings.append(
            "Exposed skills are not in generated registry: " + ", ".join(exposed_missing)
        )

    gates_missing = sorted({skill for skill in approval_gates if skill not in registry_tools})
    if gates_missing:
        warnings.append(
            "Approval gates are not in generated registry: " + ", ".join(gates_missing)
        )

    for skill in exposed_known:
        tool = registry_tools[skill]
        if tool.get("approval_required") and skill not in approval_gates:
            errors.append(f"Generated approval-required skill is exposed without gate: {skill}")

    for skill in approval_gates:
        tool = registry_tools.get(skill)
        if tool and not tool.get("approval_required"):
            errors.append(f"Gateway gate marks non-approval generated skill as gated: {skill}")

    scenario_skills = _collect_scenario_skills(gateway_path.parent.parent, gateway.get("scenarios", []))
    scenario_missing = sorted(skill for skill in scenario_skills if skill not in registry_tools)
    if scenario_missing:
        warnings.append(
            "Scenario skills are not in generated registry: " + ", ".join(scenario_missing)
        )

    if strict and warnings:
        errors.extend(f"STRICT: {warning}" for warning in warnings)

    return {
        "gateway": str(gateway_path),
        "registry": str(registry_path),
        "registry_tools": len(registry_tools),
        "exposed_known": len(exposed_known),
        "exposed_missing": exposed_missing,
        "scenario_skills": sorted(scenario_skills),
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def _resolve_registry_path(gateway_path: Path, registry_value: str) -> Path:
    raw = Path(registry_value)
    if raw.is_absolute():
        return raw
    aiagent_root = gateway_path.parent.parent
    return (aiagent_root / raw).resolve()


def _load_registry(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yml", ".yaml"}:
        return yaml.safe_load(text)
    return json.loads(text)


def _check_duplicates(label: str, values: list[str], errors: list[str]) -> None:
    seen: set[str] = set()
    duplicates = sorted({value for value in values if value in seen or seen.add(value)})
    if duplicates:
        errors.append(f"{label} has duplicates: {', '.join(duplicates)}")


def _collect_scenario_skills(aiagent_root: Path, scenarios: list[dict[str, Any]]) -> set[str]:
    skills: set[str] = set()
    for scenario in scenarios:
        path_value = scenario.get("path")
        if not path_value:
            continue
        path = (aiagent_root / path_value).resolve()
        if path.suffix.lower() not in {".yml", ".yaml"} or not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for step in data.get("steps", []):
            skill = step.get("skill")
            if skill:
                skills.add(skill)
    return skills


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AiAgent gateway/registry contract.")
    parser.add_argument("--gateway", type=Path, default=Path("aiagent/config/gateway.yml"))
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    result = check_contract(args.gateway, strict=args.strict)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"registry_tools={result['registry_tools']} exposed_known={result['exposed_known']}")
        for warning in result["warnings"]:
            print(f"WARN {warning}")
        for error in result["errors"]:
            print(f"FAIL {error}")
        if result["ok"]:
            print("OK AiAgent contract")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
