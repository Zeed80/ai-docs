#!/usr/bin/env python3
"""Generate AiAgent skill YAML from FastAPI Pydantic schemas.

Pydantic schemas are the single source of truth.
This script reads FastAPI routes and generates aiagent/skills/_registry.yml.

Fixes vs original:
- Extracts ALL body params (not just first) and merges their schemas
- Extracts path parameters from route.path
- Detects approval gates by cross-referencing gateway_config (not keywords)
- Registry version bumped to 2
"""

import json
import re
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "backend"))

from app.main import app  # noqa: E402


def _path_params(path: str) -> list[str]:
    """Extract {param} names from a URL path."""
    return re.findall(r"\{(\w+)\}", path)


def _merge_schemas(schemas: list[dict]) -> dict:
    """Merge multiple JSON schemas into one (union of properties)."""
    if not schemas:
        return {}
    if len(schemas) == 1:
        return schemas[0]
    merged: dict = {"type": "object", "properties": {}, "required": []}
    for schema in schemas:
        props = schema.get("properties") or {}
        merged["properties"].update(props)
        merged["required"] = list(
            set(merged["required"]) | set(schema.get("required") or [])
        )
    return merged


def _load_approval_gates() -> set[str]:
    """Load actual approval gates from gateway_config (authoritative source)."""
    try:
        from app.ai.gateway_config import gateway_config
        return set(gateway_config.approval_gates or [])
    except Exception:
        return set()


def extract_skills() -> list[dict]:
    """Extract skill definitions from FastAPI routes."""
    approval_gates = _load_approval_gates()
    skills = []

    for route in app.routes:
        if not hasattr(route, "methods"):
            continue
        if not route.path.startswith("/api/"):
            continue

        method = next(iter(route.methods - {"HEAD", "OPTIONS"}), None)
        if not method:
            continue

        # Build skill id from route name
        skill_id = route.name

        # Map skill_id → dot-notation name (e.g. invoice__list → invoice.list)
        skill_name = skill_id.replace("__", ".")

        skill: dict = {
            "id": skill_id,
            "name": skill_name,
            "method": method,
            "path": route.path,
            "description": (route.description or route.name or "").strip().split("\n")[0],
            "approval_gate": skill_name in approval_gates,
        }

        # --- Path parameters ---
        path_params = _path_params(route.path)
        if path_params:
            skill["path_params"] = path_params

        # --- Body parameters (all, merged) ---
        if hasattr(route, "dependant"):
            dep = route.dependant
            body_schemas: list[dict] = []
            for param in (dep.body_params or []):
                if hasattr(param, "type_") and hasattr(param.type_, "model_json_schema"):
                    body_schemas.append(param.type_.model_json_schema())
            if body_schemas:
                skill["input_schema"] = _merge_schemas(body_schemas)

        # --- Response model ---
        if hasattr(route, "response_model") and route.response_model:
            model = route.response_model
            if hasattr(model, "model_json_schema"):
                skill["output_schema"] = model.model_json_schema()
            elif hasattr(model, "__args__"):
                # Handle list[Model] etc.
                for arg in getattr(model, "__args__", []):
                    if hasattr(arg, "model_json_schema"):
                        skill["output_schema"] = {"type": "array", "items": arg.model_json_schema()}
                        break

        skills.append(skill)

    return skills


def generate_yaml(skills: list[dict]) -> str:
    """Generate YAML skill registry v2."""
    import yaml

    registry: dict = {
        "version": "2",
        "gateway_url": "http://backend:8000",
        "policy": {
            "unknown_tools": "deny",
            "approval_gate_required": True,
        },
        "skills": {},
    }

    for skill in skills:
        entry: dict = {
            "method": skill["method"],
            "path": skill["path"],
            "description": skill["description"],
        }
        if skill.get("path_params"):
            entry["path_params"] = skill["path_params"]
        if "input_schema" in skill:
            entry["input_schema"] = skill["input_schema"]
        if "output_schema" in skill:
            entry["output_schema"] = skill["output_schema"]
        if skill.get("approval_gate"):
            entry["approval_gate"] = True

        registry["skills"][skill["id"]] = entry

    return yaml.dump(registry, allow_unicode=True, default_flow_style=False, sort_keys=False)


def main():
    skills = extract_skills()
    gate_count = sum(1 for s in skills if s.get("approval_gate"))
    print(f"Found {len(skills)} skills from FastAPI routes ({gate_count} approval gates)")

    output_dir = Path(__file__).parent.parent.parent / "aiagent" / "skills"
    output_dir.mkdir(parents=True, exist_ok=True)
    registry_path = output_dir / "_registry.yml"

    try:
        yaml_content = generate_yaml(skills)
        registry_path.write_text(yaml_content, encoding="utf-8")
        print(f"Written to {registry_path}")
    except ImportError:
        json_path = output_dir / "_registry.json"
        json_content = json.dumps(
            [{"id": s["id"], "name": s["name"], "method": s["method"],
              "path": s["path"], "description": s["description"],
              "approval_gate": s.get("approval_gate", False)} for s in skills],
            indent=2,
            ensure_ascii=False,
        )
        json_path.write_text(json_content, encoding="utf-8")
        print(f"PyYAML not installed, written JSON to {json_path}")

    print("\nSummary:")
    for skill in skills:
        gate_marker = " [APPROVAL GATE]" if skill.get("approval_gate") else ""
        path_marker = f" path_params={skill['path_params']}" if skill.get("path_params") else ""
        print(f"  {skill['method']:6s} {skill['path']:50s} {skill['id']}{gate_marker}{path_marker}")


if __name__ == "__main__":
    main()
