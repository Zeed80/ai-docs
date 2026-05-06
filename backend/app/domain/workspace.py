"""In-memory workspace block store for agent rich output."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

_BLOCKS: dict[str, dict[str, Any]] = {}


def upsert_workspace_block(block_id: str, block: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    existing = _BLOCKS.get(block_id)
    stored = {
        **block,
        "id": block_id,
        "created_at": existing.get("created_at") if existing else now,
        "updated_at": now,
    }
    _BLOCKS[block_id] = stored
    return stored


def append_workspace_block(block: dict[str, Any]) -> dict[str, Any]:
    block_id = str(block.get("id") or f"workspace:{len(_BLOCKS) + 1}")
    return upsert_workspace_block(block_id, block)


def list_workspace_blocks() -> list[dict[str, Any]]:
    return sorted(_BLOCKS.values(), key=lambda item: str(item.get("updated_at")), reverse=True)


def get_workspace_block(block_id: str) -> dict[str, Any] | None:
    return _BLOCKS.get(block_id)


def delete_workspace_block(block_id: str) -> bool:
    return _BLOCKS.pop(block_id, None) is not None


def clear_workspace_blocks() -> None:
    _BLOCKS.clear()
