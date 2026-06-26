from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

DEFAULT_GATEWAY = ROOT / "aiagent" / "config" / "gateway.yml"
DEFAULT_REGISTRY = ROOT / "aiagent" / "skills" / "_registry.yml"
DEFAULT_CAPABILITIES = ROOT / "aiagent" / "skills" / "capabilities.yml"


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
    capabilities_path: Path = DEFAULT_CAPABILITIES,
    *,
    strict: bool = False,
) -> dict[str, Any]:
    gateway = _load_yaml(gateway_path)
    registry = _load_yaml(registry_path)
    tools = _registry_tools(registry)
    tool_names = set(tools)

    skills_cfg = gateway.get("skills") or {}
    skills_mode = str(skills_cfg.get("mode") or "").strip().lower()
    legacy_errors_are_warnings = skills_mode == "capabilities" and not strict
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
        message = f"gateway exposes unknown tools: {exposed_missing}"
        (warnings if legacy_errors_are_warnings else errors).append(message)

    gates_missing = sorted(approval_gates - tool_names)
    if gates_missing:
        message = f"approval gates reference unknown tools: {gates_missing}"
        (warnings if legacy_errors_are_warnings else errors).append(message)

    gates_not_exposed = sorted(approval_gates - exposed)
    if gates_not_exposed:
        message = f"approval gates are not exposed: {gates_not_exposed}"
        (warnings if legacy_errors_are_warnings else errors).append(message)

    missing_required_gates = sorted(approval_required - approval_gates)
    if missing_required_gates:
        message = f"approval_required tools missing gates: {missing_required_gates}"
        (warnings if legacy_errors_are_warnings else errors).append(message)

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

    cap_errors, cap_warnings, cap_stats = check_capability_contract(
        capabilities_path=capabilities_path,
        strict=strict,
    )
    errors.extend(cap_errors)
    warnings.extend(cap_warnings)

    return {
        "ok": not errors,
        "strict": strict,
        "registry_tools": len(tool_names),
        "exposed_tools": len(exposed),
        "approval_gates": len(approval_gates),
        **cap_stats,
        "errors": errors,
        "warnings": warnings,
    }


def check_capability_contract(
    *,
    capabilities_path: Path = DEFAULT_CAPABILITIES,
    strict: bool = False,
) -> tuple[list[str], list[str], dict[str, int]]:
    """Validate broad capability-mode contract against the Python dispatcher."""
    try:
        from app.api.capability_router import _DISPATCH, _SPECIAL_CAPABILITIES
        from app.ai.capability_manifest import load_capability_manifest
        from app.ai.policy_engine import classify_capability_action_risk
    except Exception as exc:  # pragma: no cover - import failure is surfaced to CLI
        return [f"cannot import capability dispatcher/policy: {exc}"], [], {
            "capabilities": 0,
            "capability_actions": 0,
            "capability_gate_actions": 0,
        }

    manifest = load_capability_manifest(capabilities_path)
    capabilities = manifest.by_name
    dispatcher_caps = set(_DISPATCH)
    yaml_caps = set(capabilities)

    errors: list[str] = []
    warnings: list[str] = []

    missing_in_yaml = sorted(dispatcher_caps - yaml_caps)
    if missing_in_yaml:
        errors.append(f"dispatcher capabilities missing from capabilities.yml: {missing_in_yaml}")

    special_caps = set(_SPECIAL_CAPABILITIES)
    missing_in_dispatcher = sorted(yaml_caps - dispatcher_caps - special_caps)
    if missing_in_dispatcher:
        errors.append(f"capabilities.yml references unknown dispatcher capabilities: {missing_in_dispatcher}")

    gate_count = 0
    action_count = 0
    for cap_name in sorted(yaml_caps & dispatcher_caps):
        dispatcher_actions = set(_DISPATCH[cap_name])
        action_count += len(dispatcher_actions)
        gate_actions = set(capabilities[cap_name].gate_actions)
        gate_count += len(gate_actions)

        unknown_gates = sorted(gate_actions - dispatcher_actions)
        if unknown_gates:
            errors.append(f"{cap_name} gate_actions reference unknown actions: {unknown_gates}")

        risky_not_gated = sorted(
            action
            for action in dispatcher_actions
            if classify_capability_action_risk(action) == "high"
            and action not in gate_actions
        )
        if risky_not_gated:
            errors.append(f"{cap_name} risky actions missing gate_actions: {risky_not_gated}")

        gated_low_risk = sorted(
            action
            for action in gate_actions & dispatcher_actions
            if classify_capability_action_risk(action) != "high"
        )
        if gated_low_risk:
            warnings.append(
                f"{cap_name} gates low-risk actions manually: {gated_low_risk}"
            )

    return errors, warnings, {
        "capabilities": len(yaml_caps),
        "capability_actions": action_count,
        "capability_gate_actions": gate_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Check AiAgent registry/gateway contract.")
    parser.add_argument("--gateway", type=Path, default=DEFAULT_GATEWAY)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--capabilities", type=Path, default=DEFAULT_CAPABILITIES)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = check_contract(
        args.gateway,
        args.registry,
        capabilities_path=args.capabilities,
        strict=args.strict,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(
            "OK aiagent contract: "
            f"{result['registry_tools']} registry tools, "
            f"{result['exposed_tools']} exposed, "
            f"{result['approval_gates']} approval gates, "
            f"{result['capabilities']} capabilities, "
            f"{result['capability_actions']} capability actions"
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
