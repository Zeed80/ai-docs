#!/usr/bin/env python3
"""Generate OpenClaw skill YAML from FastAPI Pydantic schemas.

Pydantic schemas are the single source of truth.
This script reads FastAPI routes and generates openclaw/skills/*.yml.
"""

import json
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "backend"))

from app.main import app  # noqa: E402


def extract_skills() -> list[dict]:
    """Extract skill definitions from FastAPI routes."""
    skills = []
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue

        # Only API routes
        if not route.path.startswith("/api/"):
            continue

        method = next(iter(route.methods - {"HEAD", "OPTIONS"}), None)
        if not method:
            continue

        # Build skill definition
        skill = {
            "id": route.name,
            "method": method,
            "path": route.path,
            "description": (route.description or route.name or "").strip().split("\n")[0],
        }

        # Extract input/output schemas from endpoint
        if hasattr(route, "dependant"):
            dep = route.dependant
            if dep.body_params:
                for param in dep.body_params:
                    if hasattr(param, "type_") and hasattr(param.type_, "model_json_schema"):
                        skill["input_schema"] = param.type_.model_json_schema()
                        break

        if hasattr(route, "response_model") and route.response_model:
            model = route.response_model
            if hasattr(model, "model_json_schema"):
                skill["output_schema"] = model.model_json_schema()

        skills.append(skill)

    return skills


def generate_yaml(skills: list[dict]) -> str:
    """Generate YAML skill registry."""
    import yaml

    registry = {
        "version": "1.0",
        "gateway_url": "http://backend:8000",
        "skills": {},
    }

    for skill in skills:
        registry["skills"][skill["id"]] = {
            "method": skill["method"],
            "path": skill["path"],
            "description": skill["description"],
        }
        if "input_schema" in skill:
            registry["skills"][skill["id"]]["input_schema"] = skill["input_schema"]
        if "output_schema" in skill:
            registry["skills"][skill["id"]]["output_schema"] = skill["output_schema"]

    return yaml.dump(registry, allow_unicode=True, default_flow_style=False, sort_keys=False)


def main():
    skills = extract_skills()
    print(f"Found {len(skills)} skills from FastAPI routes")

    # Write registry
    output_dir = Path(__file__).parent.parent.parent / "openclaw" / "skills"
    output_dir.mkdir(parents=True, exist_ok=True)

    registry_path = output_dir / "_registry.yml"

    try:
        yaml_content = generate_yaml(skills)
        registry_path.write_text(yaml_content, encoding="utf-8")
        print(f"Written to {registry_path}")
    except ImportError:
        # Fallback to JSON if PyYAML not available
        json_path = output_dir / "_registry.json"
        json_content = json.dumps(
            [{"id": s["id"], "method": s["method"], "path": s["path"], "description": s["description"]} for s in skills],
            indent=2,
            ensure_ascii=False,
        )
        json_path.write_text(json_content, encoding="utf-8")
        print(f"PyYAML not installed, written JSON to {json_path}")

    # Print summary
    for skill in skills:
        gate = " [APPROVAL GATE]" if "approve" in skill["id"] or "reject" in skill["id"] or "send" in skill["id"] or "decide" in skill["id"] or "resolve" in skill["id"] or "activate" in skill["id"] else ""
        print(f"  {skill['method']:6s} {skill['path']:50s} {skill['id']}{gate}")


if __name__ == "__main__":
    main()
