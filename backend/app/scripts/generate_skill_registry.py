#!/usr/bin/env python3
"""Generate OpenClaw skill registry YAML from FastAPI Pydantic schemas.

Reads all APIRouter endpoints and generates skill definitions
compatible with OpenClaw Gateway tool format.

Usage:
    python -m app.scripts.generate_skill_registry
"""

import importlib
import inspect
import json
import sys
from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel

ROUTERS = {
    "documents": ("app.api.documents", "/api/documents"),
    "invoices": ("app.api.invoices", "/api/invoices"),
    "email": ("app.api.email", "/api/email"),
    "approvals": ("app.api.approvals", "/api/approvals"),
    "search": ("app.api.search", "/api/search"),
    "normalization": ("app.api.normalization", "/api/normalization"),
    "tables": ("app.api.tables", "/api/tables"),
    "suppliers": ("app.api.suppliers", "/api/suppliers"),
    "collections": ("app.api.collections", "/api/collections"),
    "anomalies": ("app.api.anomalies", "/api/anomalies"),
    "compare": ("app.api.compare", "/api/compare"),
    "calendar": ("app.api.calendar", "/api/calendar"),
    "dashboard": ("app.api.dashboard", "/api/dashboard"),
    "graph": ("app.api.graph", "/api/graph"),
    "memory": ("app.api.memory", "/api/memory"),
    "technology": ("app.api.technology", "/api/technology"),
    "quarantine": ("app.api.quarantine", "/api/quarantine"),
    "warehouse": ("app.api.warehouse", "/api/warehouse"),
    "procurement": ("app.api.procurement", "/api"),
    "payments": ("app.api.payments", "/api"),
    "boms": ("app.api.boms", "/api"),
    "ntd": ("app.api.ntd", "/api"),
    "canvas": ("app.api.canvas", "/api/canvas"),
    "mailbox": ("app.api.mailbox", "/api/mailbox"),
    "email_templates": ("app.api.email_templates", "/api/email-templates"),
}

APPROVAL_GATES = {
    "invoice.approve",
    "invoice.reject",
    "invoice.bulk_delete",
    "email.send",
    "anomaly.resolve",
    "table.apply_diff",
    "norm.activate_rule",
    "compare.decide",
    "warehouse.confirm_receipt",
    "payment.mark_paid",
    "procurement.send_rfq",
    "bom.approve",
    "bom.create_purchase_request",
    "tech.process_plan_approve",
    "tech.norm_estimate_approve",
    "tech.learning_rule_activate",
}

SKILL_ALIASES = {
    "search.nl_to_query": ["search.nl"],
}


def extract_skill_name(docstring: str | None) -> str | None:
    """Extract 'Skill: xxx.yyy' from endpoint docstring."""
    if not docstring:
        return None
    for line in docstring.split("\n"):
        line = line.strip()
        if line.startswith("Skill:"):
            parts = line.split("—")[0].replace("Skill:", "").strip()
            return parts
    return None


def get_request_schema(func) -> dict | None:
    """Get JSON schema from the request body Pydantic model."""
    sig = inspect.signature(func)
    for param in sig.parameters.values():
        if (
            param.annotation
            and isinstance(param.annotation, type)
            and issubclass(param.annotation, BaseModel)
        ):
            return param.annotation.model_json_schema()
    return None


def get_response_schema(func) -> dict | None:
    """Get JSON schema from response_model if available."""
    # Check for response_model in route decorator
    return None


def generate_registry() -> dict:
    """Generate the full skill registry."""
    tools = []

    for category, (module_path, prefix) in ROUTERS.items():
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            print(f"Warning: Could not import {module_path}: {e}", file=sys.stderr)
            continue

        router: APIRouter = getattr(module, "router", None)
        if not router:
            continue

        for route in router.routes:
            if not hasattr(route, "endpoint"):
                continue

            skill_name = extract_skill_name(route.endpoint.__doc__)
            if not skill_name:
                continue

            # Build full path
            path = prefix + route.path
            methods = list(route.methods) if hasattr(route, "methods") else ["GET"]
            method = [m for m in methods if m != "HEAD"][0] if methods else "GET"

            # Extract parameter schema
            request_schema = get_request_schema(route.endpoint)

            skill = {
                "name": skill_name,
                "description": (route.endpoint.__doc__ or "").split("\n")[0].strip(),
                "category": category,
                "method": method,
                "path": path,
                "approval_required": skill_name in APPROVAL_GATES,
            }

            if request_schema:
                skill["parameters"] = request_schema

            tools.append(skill)
            for alias in SKILL_ALIASES.get(skill_name, []):
                alias_skill = {**skill, "name": alias}
                tools.append(alias_skill)

    return {
        "version": 1,
        "source": "fastapi_docstrings",
        "policy": {
            "default": "deny",
            "unknown_tools": "deny",
            "confidential_default": True,
            "external_actions_require_approval": True,
        },
        "tools": tools,
    }


def main():
    registry = generate_registry()

    output_path = (
        Path(__file__).parent.parent.parent.parent
        / "openclaw"
        / "skills"
        / "_registry.yml"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(registry, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"Generated {len(registry['tools'])} skills → {output_path}")

    # Also output JSON for reference
    json_path = output_path.with_suffix(".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)

    print(f"JSON copy → {json_path}")


if __name__ == "__main__":
    main()
