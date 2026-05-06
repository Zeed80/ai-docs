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
