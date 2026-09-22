"""Deterministic verification for explicitly supported recipient artifacts.

The registry is data-only: it cannot contain model-selected or dynamically
imported callbacks.  Dispatch below is deliberately exhaustive over the two
recipient/database pairs reviewed in E07/E08.  A verdict is evidence about one
observed artifact version only; it never authorizes replay, resume, or work
order completion.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.auth.models import UserInfo, UserRole
from app.config import settings
from app.db.agent_runtime_models import ChatLogicalAction
from app.db.models import AgentTask, InventoryItem, WorkArtifact, WorkOrder, WorkStepAttempt
from app.domain.chat_action_journal import digest

ARTIFACT_VERDICT_VERSION = "artifact-verifier-v1"
ARTIFACT_DESCRIPTOR_VERSION = 1


@dataclass(frozen=True, slots=True)
class ArtifactVerifierSpec:
    operation: str
    artifact_type: str
    scope: str
    evidence_source: str
    required_role: UserRole | None = None


# This is intentionally an explicit allowlist with no callable values.  Adding
# a recipient requires a reviewed spec and a hard-coded snapshot branch below.
ARTIFACT_VERIFIER_REGISTRY: dict[tuple[str, str], ArtifactVerifierSpec] = {
    ("agent_control.task_propose", "agent_task"): ArtifactVerifierSpec(
        operation="agent_control.task_propose",
        artifact_type="agent_task",
        scope="agent_task_database_snapshot",
        evidence_source="database:agent_tasks",
        required_role=UserRole.admin,
    ),
    ("warehouse.update_item", "inventory_item"): ArtifactVerifierSpec(
        operation="warehouse.update_item",
        artifact_type="inventory_item",
        scope="inventory_item_database_snapshot",
        evidence_source="database:inventory_items",
    ),
}
_SPEC_BY_OPERATION = {spec.operation: spec for spec in ARTIFACT_VERIFIER_REGISTRY.values()}
_DATETIME_ADAPTER = TypeAdapter(datetime)


def supported_artifact_verifiers() -> tuple[tuple[str, str], ...]:
    return tuple(sorted(ARTIFACT_VERIFIER_REGISTRY))


def _json_datetime(value: datetime) -> str:
    return _DATETIME_ADAPTER.dump_python(value, mode="json")


def _agent_task_snapshot(task: AgentTask) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "objective": task.objective,
        "description": task.description,
        "role": task.role,
        "status": task.status,
        "team_id": str(task.team_id) if task.team_id else None,
        "output": task.output,
        "metadata": task.metadata_,
        "created_at": _json_datetime(task.created_at),
        "updated_at": _json_datetime(task.updated_at),
    }


def _inventory_item_snapshot(item: InventoryItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "sku": item.sku,
        "name": item.name,
        "unit": item.unit,
        "current_qty": item.current_qty,
        "min_qty": item.min_qty,
        "location": item.location,
        "is_low_stock": bool(item.min_qty and item.current_qty < item.min_qty),
        "created_at": _json_datetime(item.created_at),
        "updated_at": _json_datetime(item.updated_at),
    }


def _content_snapshot(spec: ArtifactVerifierSpec, snapshot: dict[str, Any]) -> dict[str, Any]:
    if spec.artifact_type == "agent_task":
        fields = (
            "id",
            "objective",
            "description",
            "role",
            "status",
            "team_id",
            "output",
            "metadata",
        )
        return {field: snapshot[field] for field in fields}
    if spec.artifact_type == "inventory_item":
        fields = ("id", "sku", "name", "unit", "current_qty", "min_qty", "location", "is_low_stock")
        return {field: snapshot[field] for field in fields}
    raise AssertionError("unsupported artifact registry entry")


async def record_receipt_artifact(
    db: Any,
    *,
    action: ChatLogicalAction,
    operation: str,
    response: dict[str, Any],
    response_digest: str,
    artifact_id: str,
    artifact_revision: str,
) -> WorkArtifact:
    """Add the versioned descriptor in the same transaction as its receipt."""
    spec = _SPEC_BY_OPERATION.get(operation)
    if spec is None:
        raise ValueError("Unsupported recipient artifact type")
    try:
        response_id = str(uuid.UUID(str(response["id"])))
        response_revision = response["updated_at"]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Recipient response has no stable artifact version") from None
    if (
        response_id != artifact_id
        or response_revision != artifact_revision
        or digest(response) != response_digest
    ):
        raise ValueError("Recipient artifact descriptor binding mismatch")
    attempt = await db.get(WorkStepAttempt, action.attempt_id)
    descriptor = WorkArtifact(
        work_order_id=action.work_order_id,
        step_id=attempt.step_id if attempt is not None else None,
        artifact_type=spec.artifact_type,
        name=f"{spec.artifact_type}:{artifact_id}",
        content_hash=response_digest,
        content_type="application/json",
        metadata_={
            "descriptor_version": ARTIFACT_DESCRIPTOR_VERSION,
            "logical_action_id": str(action.id),
            "operation": operation,
            "recipient_artifact_id": artifact_id,
            "artifact_revision": artifact_revision,
            "receipt_version": 1,
            "evidence_source": spec.evidence_source,
        },
    )
    db.add(descriptor)
    await db.flush()
    return descriptor


def _base_verdict(action: ChatLogicalAction, *, observed_at: str) -> dict[str, Any]:
    return {
        "verifier_version": ARTIFACT_VERDICT_VERSION,
        "action_id": str(action.id),
        "operation": None,
        "artifact_type": None,
        "artifact_id": None,
        "artifact_version": None,
        "artifact_hash": None,
        "expected_artifact_version": None,
        "expected_artifact_hash": None,
        "scope": "recipient_artifact",
        "observed_at": observed_at,
        "evidence_source": "recipient_receipt",
        "work_artifact_id": None,
        "external_reference": None,
        "status": "inconclusive",
        "reason": "recipient_receipt_missing",
        "can_replay": False,
        # E10 has not defined a continuation transition for any verdict.  Keep
        # the existing explicit denial in this response contract; a verifier
        # observation cannot silently become a resume capability.
        "can_resume": False,
    }


def _seal_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(verdict)
    # A bare digest would let a client fabricate an otherwise plausible
    # verdict. This signature is only an integrity binding; E09 deliberately
    # does not consume verdicts as authority for completion or continuation.
    sealed["verdict_digest"] = hmac.new(
        settings.app_secret_key.encode("utf-8"),
        digest(sealed).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return sealed


def _opaque_reference(arguments: dict[str, Any]) -> str | None:
    """Return a declared external reference as evidence text, never as a target."""
    reference = arguments.get("reference")
    return reference if isinstance(reference, str) else None


def validate_artifact_verdict(verdict: dict[str, Any]) -> bool:
    """Reject forged, unversioned, or internally inconsistent verdict data."""
    if not isinstance(verdict, dict):
        return False
    supplied_digest = verdict.get("verdict_digest")
    unsigned = {key: value for key, value in verdict.items() if key != "verdict_digest"}
    expected_digest = hmac.new(
        settings.app_secret_key.encode("utf-8"),
        digest(unsigned).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(supplied_digest, str) or not hmac.compare_digest(
        supplied_digest, expected_digest
    ):
        return False
    spec = ARTIFACT_VERIFIER_REGISTRY.get(
        (str(verdict.get("operation")), str(verdict.get("artifact_type")))
    )
    if spec is None or verdict.get("verifier_version") != ARTIFACT_VERDICT_VERSION:
        return False
    if verdict.get("scope") != spec.scope or verdict.get("evidence_source") != spec.evidence_source:
        return False
    if verdict.get("status") == "matched":
        required = (
            "artifact_id",
            "artifact_version",
            "artifact_hash",
            "expected_artifact_version",
            "expected_artifact_hash",
            "observed_at",
        )
        if any(not isinstance(verdict.get(key), str) or not verdict[key] for key in required):
            return False
        if (
            verdict["artifact_version"] != verdict["expected_artifact_version"]
            or verdict["artifact_hash"] != verdict["expected_artifact_hash"]
        ):
            return False
    return verdict.get("can_replay") is False and verdict.get("can_resume") is False


def verdict_proves_current_artifact(
    verdict: dict[str, Any], current_observation: dict[str, Any]
) -> bool:
    """True only for the same exact artifact version in a fresh matched observation."""
    if not validate_artifact_verdict(verdict) or not validate_artifact_verdict(current_observation):
        return False
    if verdict.get("status") != "matched" or current_observation.get("status") != "matched":
        return False
    binding_fields = (
        "operation",
        "artifact_type",
        "artifact_id",
        "artifact_version",
        "artifact_hash",
        "scope",
        "evidence_source",
    )
    return all(verdict.get(key) == current_observation.get(key) for key in binding_fields)


async def _artifact_descriptor(db: Any, action: ChatLogicalAction, spec: ArtifactVerifierSpec):
    rows = list(
        (
            await db.scalars(
                select(WorkArtifact).where(
                    WorkArtifact.work_order_id == action.work_order_id,
                    WorkArtifact.artifact_type == spec.artifact_type,
                    WorkArtifact.metadata_["logical_action_id"].as_string() == str(action.id),
                )
            )
        ).all()
    )
    return rows


async def _recipient_snapshot(db: Any, spec: ArtifactVerifierSpec, artifact_id: uuid.UUID):
    if spec.artifact_type == "agent_task":
        row = await db.get(AgentTask, artifact_id, populate_existing=True)
        return _agent_task_snapshot(row) if row is not None else None
    if spec.artifact_type == "inventory_item":
        row = await db.get(InventoryItem, artifact_id, populate_existing=True)
        return _inventory_item_snapshot(row) if row is not None else None
    raise AssertionError("unsupported artifact registry entry")


async def verify_action_artifact(
    db: Any,
    *,
    action: ChatLogicalAction,
    user: UserInfo,
) -> dict[str, Any]:
    """Observe one exact recipient artifact without changing workflow state."""
    from app.domain.action_receipts import read_receipt

    observed_at = datetime.now(UTC).isoformat()
    base = _base_verdict(action, observed_at=observed_at)

    # Owner isolation happens before receipt, descriptor, or recipient content.
    order = await db.get(WorkOrder, action.work_order_id)
    if order is None or order.owner_key != user.sub:
        raise HTTPException(404, "Logical action not found")

    request = action.request if isinstance(action.request, dict) else {}
    arguments = request.get("arguments")
    try:
        arguments = dict(arguments) if isinstance(arguments, dict) else json.loads(arguments)
    except (TypeError, ValueError):
        arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    base["external_reference"] = _opaque_reference(arguments)
    declared_operation = (
        f"{request.get('name', '').replace('__', '.')}.{arguments.get('action', '')}"
    )
    declared_spec = _SPEC_BY_OPERATION.get(declared_operation)
    if (
        declared_spec
        and declared_spec.required_role is not None
        and declared_spec.required_role not in user.roles
    ):
        raise HTTPException(403, "Current artifact verification is not permitted")

    receipt = await read_receipt(db, action)
    if receipt is None:
        return _seal_verdict(base)
    operation = receipt.get("operation")
    spec = _SPEC_BY_OPERATION.get(operation)
    if spec is None:
        return _seal_verdict(
            {
                **base,
                "operation": operation,
                "artifact_id": receipt.get("artifact_id"),
                "expected_artifact_version": receipt.get("artifact_revision"),
                "expected_artifact_hash": receipt.get("response_digest"),
                "reason": "unsupported_recipient_artifact",
            }
        )
    if spec.required_role is not None and spec.required_role not in user.roles:
        raise HTTPException(403, "Current artifact verification is not permitted")

    verdict = {
        **base,
        "operation": spec.operation,
        "artifact_type": spec.artifact_type,
        "artifact_id": receipt.get("artifact_id"),
        "expected_artifact_version": receipt.get("artifact_revision"),
        "expected_artifact_hash": receipt.get("response_digest"),
        "scope": spec.scope,
        "evidence_source": spec.evidence_source,
        "receipt_response_digest": receipt.get("response_digest"),
    }
    try:
        artifact_id = uuid.UUID(str(receipt["artifact_id"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(409, "Recipient artifact binding is invalid") from None

    descriptors = await _artifact_descriptor(db, action, spec)
    if len(descriptors) != 1:
        verdict["reason"] = (
            "artifact_descriptor_missing" if not descriptors else "artifact_descriptor_ambiguous"
        )
        return _seal_verdict(verdict)
    descriptor = descriptors[0]
    metadata = descriptor.metadata_ if isinstance(descriptor.metadata_, dict) else {}
    verdict["work_artifact_id"] = str(descriptor.id)
    # URI is opaque evidence text.  It is never dereferenced by this module.
    verdict["external_reference"] = descriptor.uri or base["external_reference"]
    if metadata.get("descriptor_version") != ARTIFACT_DESCRIPTOR_VERSION:
        verdict["reason"] = "artifact_descriptor_version_missing"
        return _seal_verdict(verdict)
    expected_descriptor = {
        "logical_action_id": str(action.id),
        "operation": spec.operation,
        "recipient_artifact_id": str(artifact_id),
        "artifact_revision": receipt["artifact_revision"],
        "receipt_version": receipt["receipt_version"],
        "evidence_source": spec.evidence_source,
    }
    if (
        any(metadata.get(key) != value for key, value in expected_descriptor.items())
        or descriptor.content_hash != receipt["response_digest"]
    ):
        verdict["reason"] = "artifact_descriptor_binding_mismatch"
        return _seal_verdict(verdict)

    try:
        snapshot = await _recipient_snapshot(db, spec, artifact_id)
    except SQLAlchemyError:
        verdict["reason"] = "recipient_unavailable"
        return _seal_verdict(verdict)
    if snapshot is None:
        verdict.update(
            {
                "status": "missing",
                "reason": "authoritative_local_record_missing",
                "current_content_digest": None,
            }
        )
        return _seal_verdict(verdict)

    current_version = snapshot["updated_at"]
    current_hash = digest(snapshot)
    content = _content_snapshot(spec, snapshot)
    expected_content = _content_snapshot(spec, receipt["response"])
    verdict.update(
        {
            "artifact_version": current_version,
            "artifact_hash": current_hash,
            "expected_content_digest": digest(expected_content),
            "current_content_digest": digest(content),
            "checked_fields": list(content),
        }
    )
    if (
        current_version == verdict["expected_artifact_version"]
        and current_hash == verdict["expected_artifact_hash"]
    ):
        verdict.update({"status": "matched", "reason": "exact_artifact_version_matched"})
    else:
        verdict.update({"status": "changed", "reason": "artifact_version_or_hash_changed"})
    return _seal_verdict(verdict)
