import uuid

from app.ai.capability_sandbox import run_capability_sandbox
from app.db.models import CapabilityProposal


def test_capability_sandbox_materializes_review_artifacts() -> None:
    proposal = CapabilityProposal(
        id=uuid.uuid4(),
        title="Workspace export helper",
        missing_capability="Need a workspace export helper",
        reason="Agent could not publish requested export",
        suggested_artifact="tool",
        risk_level="medium",
        draft={
            "tool_name": "workspace.export_helper",
            "endpoint_path": "/api/workspace/agent/export-helper",
            "method": "POST",
            "implementation_plan": ["Add a typed FastAPI endpoint."],
            "validation_plan": ["Run focused API tests."],
        },
    )

    result = run_capability_sandbox(proposal)

    assert result.ok is True
    assert result.test_status == "passed"
    assert result.audit_status == "pending"
    assert "README.md" in result.files
    assert "api_stub.py" in result.files
    assert "skill_entry.yml" in result.files
    assert "endpoint_path" in result.recognized_keys
    assert "Production files are not modified" in result.diff_preview


def test_capability_sandbox_rejects_invalid_endpoint() -> None:
    proposal = CapabilityProposal(
        id=uuid.uuid4(),
        title="Invalid helper",
        missing_capability="Need helper",
        reason="Invalid draft",
        suggested_artifact="tool",
        risk_level="medium",
        draft={
            "tool_name": "workspace.invalid_helper",
            "endpoint_path": "/not-api/helper",
            "implementation_plan": ["Bad path."],
        },
    )

    result = run_capability_sandbox(proposal)

    assert result.ok is False
    assert result.test_status == "failed"
    assert result.validation_errors == ["endpoint_path must start with /api/."]
