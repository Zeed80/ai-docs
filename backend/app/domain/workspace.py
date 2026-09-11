"""Owned workspace artifacts. PostgreSQL is authoritative; no implicit expiry."""

from __future__ import annotations

import copy
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
from app.db.agent_runtime_models import OwnedWorkspaceBlock

# Unit tests use an isolated store; never a production outage fallback.
_FALLBACK: dict[str, dict] = {}


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


def upsert_workspace_block(block_id: str, block: dict[str, Any]) -> dict[str, Any]:
    owner = _owner()
    now = _now_iso()
    existing = get_workspace_block(block_id)
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
    if settings.app_env == "test":
        _FALLBACK[f"{owner}:{block_id}"] = copy.deepcopy(stored)
    else:
        statement = (
            insert(OwnedWorkspaceBlock)
            .values(id=uuid.uuid4(), owner_key=owner, block_key=block_id, payload=stored)
            .on_conflict_do_update(
                index_elements=["owner_key", "block_key"],
                set_={"payload": stored, "updated_at": datetime.now(UTC)},
            )
        )
        with _engine().begin() as conn:
            conn.execute(statement)
    return stored


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
        return _FALLBACK.pop(f"{owner}:{block_id}", None) is not None
    with _engine().begin() as conn:
        result = conn.execute(
            delete(OwnedWorkspaceBlock).where(
                OwnedWorkspaceBlock.owner_key == owner, OwnedWorkspaceBlock.block_key == block_id
            )
        )
        return bool(result.rowcount)


def clear_workspace_blocks() -> None:
    for block in list_workspace_blocks():
        delete_workspace_block(block["id"])
