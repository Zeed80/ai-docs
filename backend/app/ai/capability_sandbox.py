"""Sandbox materialization for proposed agent capabilities.

The runner deliberately writes only under ``backend/data/agent_sandbox``.  It
does not patch production routers, registry files, or prompts; promotion remains
a separate explicit decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.db.models import CapabilityProposal

_ROOT = Path(__file__).resolve().parents[2] / "data" / "agent_sandbox"


@dataclass(frozen=True)
class CapabilitySandboxResult:
    sandbox_dir: str
    files: list[str]
    recognized_keys: list[str]
    validation_errors: list[str]
    validation_warnings: list[str]
    diff_preview: str

    @property
    def ok(self) -> bool:
        return not self.validation_errors

    @property
    def test_status(self) -> str:
        return "passed" if self.ok else "failed"

    @property
    def audit_status(self) -> str:
        return "pending" if self.ok else "failed"


def run_capability_sandbox(proposal: CapabilityProposal) -> CapabilitySandboxResult:
    """Create a reviewable sandbox package for a capability proposal."""
    draft = proposal.draft or {}
    recognized_keys = _recognized_keys(draft)
    errors, warnings = _validate_draft(draft)

    sandbox_dir = _proposal_dir(proposal)
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    files: list[Path] = []
    files.append(_write_json(sandbox_dir / "proposal.json", _proposal_payload(proposal)))
    files.append(_write_json(sandbox_dir / "draft.json", draft))
    files.append(_write_text(sandbox_dir / "README.md", _readme(proposal, errors, warnings)))
    files.append(_write_text(sandbox_dir / "implementation_plan.md", _implementation_plan(draft)))
    files.append(_write_text(sandbox_dir / "validation_plan.md", _validation_plan(draft)))

    skill_entry = _skill_entry(draft)
    if skill_entry:
        files.append(_write_text(sandbox_dir / "skill_entry.yml", yaml.safe_dump(
            skill_entry,
            allow_unicode=True,
            sort_keys=False,
        )))

    if draft.get("endpoint_path") or draft.get("tool_name"):
        files.append(_write_text(sandbox_dir / "api_stub.py", _api_stub(draft)))

    diff_preview = _diff_preview(proposal, files, errors, warnings)
    files.append(_write_text(sandbox_dir / "diff_preview.patch", diff_preview))

    rel_files = [str(path.relative_to(sandbox_dir)) for path in files]
    return CapabilitySandboxResult(
        sandbox_dir=str(sandbox_dir),
        files=rel_files,
        recognized_keys=recognized_keys,
        validation_errors=errors,
        validation_warnings=warnings,
        diff_preview=diff_preview,
    )


def _proposal_dir(proposal: CapabilityProposal) -> Path:
    digest = hashlib.sha256(str(proposal.id).encode("utf-8")).hexdigest()[:12]
    safe_title = re.sub(r"[^a-zA-Z0-9_.-]+", "-", proposal.title.lower()).strip("-")
    return _ROOT / f"{digest}-{safe_title[:48] or 'capability'}"


def _recognized_keys(draft: dict[str, Any]) -> list[str]:
    keys = {
        "tool_name",
        "endpoint_path",
        "method",
        "skill_registry_entry",
        "request_schema",
        "response_schema",
        "implementation_plan",
        "validation_plan",
        "files",
        "tests",
    }
    return sorted(key for key in keys if key in draft)


def _validate_draft(draft: dict[str, Any]) -> tuple[list[str], list[str]]:
    import ast

    errors: list[str] = []
    warnings: list[str] = []

    if not _recognized_keys(draft):
        errors.append(
            "Draft must include at least one known capability field: "
            "tool_name, endpoint_path, skill_registry_entry, implementation_plan, files, or tests."
        )

    method = str(draft.get("method") or "POST").upper()
    if method not in {"GET", "POST", "PATCH", "DELETE"}:
        errors.append(f"Unsupported HTTP method for sandbox stub: {method}.")

    endpoint_path = str(draft.get("endpoint_path") or "")
    if endpoint_path and not endpoint_path.startswith("/api/"):
        errors.append("endpoint_path must start with /api/.")

    tool_name = str(draft.get("tool_name") or "")
    if tool_name and not re.fullmatch(r"[a-zA-Z0-9_.-]+", tool_name):
        errors.append("tool_name contains unsupported characters.")

    # Validate any embedded Python code
    code = draft.get("code") or draft.get("implementation_code")
    if isinstance(code, str) and code.strip():
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            errors.append(f"Python code syntax error at line {exc.lineno}: {exc.msg}")
            tree = None
        if tree is not None:
            fn_names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            }
            if "execute" not in fn_names:
                errors.append("Generated code must define an 'execute' function (sync or async).")
            # Check for SKILL_META constant
            top_assigns = [
                node.targets[0].id
                for node in ast.walk(tree)
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
            ]
            if "SKILL_META" not in top_assigns:
                warnings.append("Generated code is missing SKILL_META dict; registry entry may be incomplete.")

    if not draft.get("implementation_plan"):
        warnings.append("implementation_plan is missing; sandbox package will be skeletal.")
    if not draft.get("validation_plan") and not draft.get("tests"):
        warnings.append("validation_plan/tests are missing; runner cannot prove behavior yet.")

    return errors, warnings


def _proposal_payload(proposal: CapabilityProposal) -> dict[str, Any]:
    return {
        "id": str(proposal.id),
        "title": proposal.title,
        "missing_capability": proposal.missing_capability,
        "reason": proposal.reason,
        "suggested_artifact": proposal.suggested_artifact,
        "risk_level": proposal.risk_level,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
        "sandbox_created_at": datetime.now(timezone.utc).isoformat(),
    }


def _skill_entry(draft: dict[str, Any]) -> dict[str, Any] | None:
    raw = draft.get("skill_registry_entry")
    if isinstance(raw, dict) and raw:
        return raw
    tool_name = draft.get("tool_name")
    endpoint_path = draft.get("endpoint_path")
    if not tool_name or not endpoint_path:
        return None
    return {
        "name": str(tool_name),
        "description": f"Skill: {tool_name} — Sandbox draft generated from capability proposal.",
        "category": "agent_generated",
        "method": str(draft.get("method") or "POST").upper(),
        "path": str(endpoint_path),
        "approval_required": False,
    }


def _readme(proposal: CapabilityProposal, errors: list[str], warnings: list[str]) -> str:
    status = "blocked" if errors else "ready"
    lines = [
        f"# {proposal.title}",
        "",
        f"Status: `{status}`",
        f"Proposal: `{proposal.id}`",
        f"Risk: `{proposal.risk_level}`",
        "",
        "## Missing Capability",
        proposal.missing_capability,
        "",
        "## Reason",
        proposal.reason,
    ]
    if errors:
        lines.extend(["", "## Validation Errors", *[f"- {item}" for item in errors]])
    if warnings:
        lines.extend(["", "## Validation Warnings", *[f"- {item}" for item in warnings]])
    return "\n".join(lines) + "\n"


def _implementation_plan(draft: dict[str, Any]) -> str:
    items = draft.get("implementation_plan")
    if not isinstance(items, list) or not items:
        items = ["Create production implementation after human approval."]
    return "# Implementation Plan\n\n" + "\n".join(f"- {item}" for item in items) + "\n"


def _validation_plan(draft: dict[str, Any]) -> str:
    items = draft.get("validation_plan") or draft.get("tests")
    if not isinstance(items, list) or not items:
        items = ["Add focused unit/API tests before promotion."]
    return "# Validation Plan\n\n" + "\n".join(f"- {item}" for item in items) + "\n"


def _api_stub(draft: dict[str, Any]) -> str:
    method = str(draft.get("method") or "POST").lower()
    endpoint = str(draft.get("endpoint_path") or "/api/generated/capability")
    tool_name = str(draft.get("tool_name") or "generated.capability")
    function_name = re.sub(r"[^a-zA-Z0-9_]+", "_", tool_name).strip("_") or "generated_capability"
    return f'''"""Sandbox-only FastAPI stub for `{tool_name}`.

Copy into a production router only after proposal approval and tests.
"""

from fastapi import APIRouter

router = APIRouter()


@router.{method}("{endpoint}")
async def {function_name}() -> dict:
    return {{
        "status": "sandbox_stub",
        "tool": "{tool_name}",
    }}
'''


def _diff_preview(
    proposal: CapabilityProposal,
    files: list[Path],
    errors: list[str],
    warnings: list[str],
) -> str:
    lines = [
        f"# Sandbox preview for {proposal.id}",
        "# Production files are not modified by this runner.",
    ]
    for path in files:
        lines.extend([
            f"--- /dev/null",
            f"+++ b/{path.name}",
            f"@@ sandbox artifact @@",
            f"+{path.name}",
        ])
    if errors:
        lines.append(f"# validation_errors={json.dumps(errors, ensure_ascii=False)}")
    if warnings:
        lines.append(f"# validation_warnings={json.dumps(warnings, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    return _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


# ── Promotion ─────────────────────────────────────────────────────────────────

_STAGING_ROOT = Path(__file__).resolve().parents[2] / "data" / "agent_staging"
_AIAGENT_ROOT = Path(
    __import__("os").environ.get(
        "AIAGENT_ROOT",
        str(Path(__file__).parent.parent.parent / "aiagent"),
    )
)
_GATEWAY_PATH = _AIAGENT_ROOT / "config" / "gateway.yml"


@dataclass(frozen=True)
class CapabilityPromoteResult:
    staging_dir: str
    files: list[str]
    skill_name: str | None
    gateway_updated: bool
    errors: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def promote_capability(proposal: CapabilityProposal) -> CapabilityPromoteResult:
    """Copy sandbox artifacts to staging and register the skill in gateway.yml."""
    errors: list[str] = []
    skill_name: str | None = None
    gateway_updated = False

    # Resolve sandbox dir
    sandbox_dir = _proposal_dir(proposal)
    staging_dir = _STAGING_ROOT / str(proposal.id)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Copy sandbox files → staging
    copied: list[str] = []
    if sandbox_dir.exists():
        for src in sandbox_dir.iterdir():
            dst = staging_dir / src.name
            dst.write_bytes(src.read_bytes())
            copied.append(src.name)
    else:
        errors.append(f"Sandbox directory not found: {sandbox_dir}")

    # Extract skill name from draft
    draft = proposal.draft or {}
    skill_entry = _skill_entry(draft)
    if skill_entry:
        skill_name = skill_entry.get("name") or str(draft.get("tool_name") or "")

    # Register in gateway.yml exposed list
    if skill_name and not errors:
        try:
            gateway_updated = _add_skill_to_gateway(skill_name)
        except Exception as exc:
            errors.append(f"Failed to update gateway.yml: {exc}")

    # Write promotion manifest to staging
    manifest = {
        "proposal_id": str(proposal.id),
        "title": proposal.title,
        "skill_name": skill_name,
        "gateway_updated": gateway_updated,
        "sandbox_dir": str(sandbox_dir),
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "errors": errors,
    }
    (staging_dir / "promotion_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    copied.append("promotion_manifest.json")

    return CapabilityPromoteResult(
        staging_dir=str(staging_dir),
        files=copied,
        skill_name=skill_name,
        gateway_updated=gateway_updated,
        errors=errors,
    )


def _add_skill_to_gateway(skill_name: str) -> bool:
    """Append skill_name to skills.exposed in gateway.yml if not already present."""
    if not _GATEWAY_PATH.exists():
        return False

    raw_text = _GATEWAY_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text) or {}
    skills_section = data.setdefault("skills", {})
    exposed: list[str] = skills_section.setdefault("exposed", [])

    if skill_name in exposed:
        return True

    exposed.append(skill_name)
    skills_section["exposed"] = exposed
    data["skills"] = skills_section

    _GATEWAY_PATH.write_text(
        yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return True
