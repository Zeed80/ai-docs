"""Owned workspace artifacts. PostgreSQL is authoritative; no implicit expiry.

E42: a write never overwrites history. Each write is a new revision kept in
``owned_workspace_block_versions``; the live row carries the latest one. A
writer that read revision N may pass ``expected_revision=N`` and gets a
conflict instead of silently replacing someone else's later change.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fastapi import HTTPException
from sqlalchemy import create_engine, delete, select
from sqlalchemy.dialects.postgresql import insert

from app.ai.actor_context import get_acting_user
from app.config import settings
from app.db.agent_runtime_models import OwnedWorkspaceBlock, OwnedWorkspaceBlockVersion

# Unit tests use an isolated store; never a production outage fallback.
_FALLBACK: dict[str, dict] = {}
_FALLBACK_VERSIONS: dict[str, list[dict]] = {}


class BlockRevisionConflict(HTTPException):
    """The block moved on since the revision the writer read."""

    def __init__(self, block_id: str, expected: int, current: int) -> None:
        super().__init__(
            409,
            detail={
                "code": "workspace_block_revision_conflict",
                "block_id": block_id,
                "expected_revision": expected,
                "current_revision": current,
            },
        )


def _content_hash(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k not in {"updated_at", "revision", "content_hash"}}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _owner() -> str:
    actor = get_acting_user()
    if actor:
        return actor
    if not settings.auth_enabled:
        from app.auth.jwt import _DEV_USER

        return _DEV_USER.sub
    raise HTTPException(403, "Workspace owner is required")


@lru_cache(maxsize=1)
def _engine():
    return create_engine(settings.database_url_sync, pool_pre_ping=True, pool_size=5)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def upsert_workspace_block(
    block_id: str, block: dict[str, Any], *, expected_revision: int | None = None
) -> dict[str, Any]:
    owner = _owner()
    now = _now_iso()
    if settings.app_env == "test":
        key = f"{owner}:{block_id}"
        existing = _FALLBACK.get(key)
        current = int(existing.get("revision") or 0) if existing else 0
        if expected_revision is not None and expected_revision != current:
            raise BlockRevisionConflict(block_id, expected_revision, current)
        stored = _stored(block_id, block, owner, now, existing, current + 1)
        _FALLBACK[key] = copy.deepcopy(stored)
        _FALLBACK_VERSIONS.setdefault(key, []).append(copy.deepcopy(stored))
        return stored
    with _engine().begin() as conn:
        # Lock the live row (creating it if new) so two writers serialize.
        conn.execute(
            insert(OwnedWorkspaceBlock)
            .values(id=uuid.uuid4(), owner_key=owner, block_key=block_id, payload={}, revision=0)
            .on_conflict_do_nothing(index_elements=["owner_key", "block_key"])
        )
        row = conn.execute(
            select(OwnedWorkspaceBlock.payload, OwnedWorkspaceBlock.revision)
            .where(
                OwnedWorkspaceBlock.owner_key == owner, OwnedWorkspaceBlock.block_key == block_id
            )
            .with_for_update()
        ).one()
        current = int(row.revision or 0)
        if expected_revision is not None and expected_revision != current:
            raise BlockRevisionConflict(block_id, expected_revision, current)
        existing = row.payload if current else None
        stored = _stored(block_id, block, owner, now, existing, current + 1)
        conn.execute(
            OwnedWorkspaceBlock.__table__.update()
            .where(
                OwnedWorkspaceBlock.owner_key == owner, OwnedWorkspaceBlock.block_key == block_id
            )
            .values(payload=stored, revision=current + 1, updated_at=datetime.now(UTC))
        )
        conn.execute(
            insert(OwnedWorkspaceBlockVersion).values(
                id=uuid.uuid4(),
                owner_key=owner,
                block_key=block_id,
                revision=current + 1,
                payload=stored,
                content_hash=stored["content_hash"],
                created_at=datetime.now(UTC),
            )
        )
    return stored


def _stored(
    block_id: str,
    block: dict[str, Any],
    owner: str,
    now: str,
    existing: dict[str, Any] | None,
    revision: int,
) -> dict[str, Any]:
    stored = json.loads(
        json.dumps(
            {
                **block,
                "id": block_id,
                "owner_key": owner,
                "created_at": existing.get("created_at") if existing else now,
                "updated_at": now,
            },
            default=str,
        )
    )
    stored["revision"] = revision
    stored["content_hash"] = _content_hash(stored)
    return stored


def list_workspace_block_versions(block_id: str) -> list[dict[str, Any]]:
    """Revision, hash and time of every version, newest first."""
    owner = _owner()
    if settings.app_env == "test":
        versions = _FALLBACK_VERSIONS.get(f"{owner}:{block_id}", [])
        rows = [(v["revision"], v["content_hash"], v["updated_at"]) for v in versions]
    else:
        with _engine().connect() as conn:
            rows = conn.execute(
                select(
                    OwnedWorkspaceBlockVersion.revision,
                    OwnedWorkspaceBlockVersion.content_hash,
                    OwnedWorkspaceBlockVersion.created_at,
                ).where(
                    OwnedWorkspaceBlockVersion.owner_key == owner,
                    OwnedWorkspaceBlockVersion.block_key == block_id,
                )
            ).all()
    return [
        {"revision": r, "content_hash": h, "created_at": str(t)}
        for r, h, t in sorted(rows, key=lambda row: row[0], reverse=True)
    ]


def get_workspace_block_version(block_id: str, revision: int) -> dict[str, Any] | None:
    owner = _owner()
    if settings.app_env == "test":
        for version in _FALLBACK_VERSIONS.get(f"{owner}:{block_id}", []):
            if version["revision"] == revision:
                return copy.deepcopy(version)
        return None
    with _engine().connect() as conn:
        return conn.execute(
            select(OwnedWorkspaceBlockVersion.payload).where(
                OwnedWorkspaceBlockVersion.owner_key == owner,
                OwnedWorkspaceBlockVersion.block_key == block_id,
                OwnedWorkspaceBlockVersion.revision == revision,
            )
        ).scalar_one_or_none()


def append_workspace_block(block: dict[str, Any]) -> dict[str, Any]:
    return upsert_workspace_block(str(block.get("id") or f"workspace:{uuid.uuid4()}"), block)


def list_workspace_blocks() -> list[dict[str, Any]]:
    owner = _owner()
    if settings.app_env == "test":
        items = [copy.deepcopy(v) for v in _FALLBACK.values() if v.get("owner_key") == owner]
    else:
        with _engine().connect() as conn:
            items = list(
                conn.execute(
                    select(OwnedWorkspaceBlock.payload).where(
                        OwnedWorkspaceBlock.owner_key == owner
                    )
                ).scalars()
            )
    return sorted(items, key=lambda b: str(b.get("updated_at", "")), reverse=True)


def get_workspace_block(block_id: str) -> dict[str, Any] | None:
    owner = _owner()
    if settings.app_env == "test":
        return copy.deepcopy(_FALLBACK.get(f"{owner}:{block_id}"))
    with _engine().connect() as conn:
        return conn.execute(
            select(OwnedWorkspaceBlock.payload).where(
                OwnedWorkspaceBlock.owner_key == owner, OwnedWorkspaceBlock.block_key == block_id
            )
        ).scalar_one_or_none()


def delete_workspace_block(block_id: str) -> bool:
    owner = _owner()
    if settings.app_env == "test":
        _FALLBACK_VERSIONS.pop(f"{owner}:{block_id}", None)
        return _FALLBACK.pop(f"{owner}:{block_id}", None) is not None
    with _engine().begin() as conn:
        result = conn.execute(
            delete(OwnedWorkspaceBlock).where(
                OwnedWorkspaceBlock.owner_key == owner, OwnedWorkspaceBlock.block_key == block_id
            )
        )
        # The owner deleting a block deletes its history too: versions are
        # protection against silent overwrite, not a retention archive.
        conn.execute(
            delete(OwnedWorkspaceBlockVersion).where(
                OwnedWorkspaceBlockVersion.owner_key == owner,
                OwnedWorkspaceBlockVersion.block_key == block_id,
            )
        )
        return bool(result.rowcount)


def clear_workspace_blocks() -> None:
    for block in list_workspace_blocks():
        delete_workspace_block(block["id"])
