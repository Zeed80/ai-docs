"""Turn-local result vault — thin context / fat storage pattern.

Large tool results (> VAULT_THRESHOLD chars) are stored in Redis with a short
TTL instead of being dumped verbatim into the conversation history.

The conversation history receives a compact *envelope*:
  - ``vault_ref``   — opaque key to retrieve full data
  - ``total``       — total record count
  - preview items   — first PREVIEW_ITEMS rows so the agent sees the schema
  - ``_vault_note`` — instruction: prefer workspace.* for display, vault for paging

The agent can retrieve more records via the ``vault`` capability
(GET /api/agent/vault/page?vault_ref=…&offset=…&limit=…).

Workspace queries don't need the vault at all: workspace.* endpoints fetch
data directly from Postgres and publish to canvas — the history only needs
the compact envelope.
"""
from __future__ import annotations

import json
import uuid
import logging

logger = logging.getLogger(__name__)

# Results larger than this (chars) are vaulted; smaller ones use _trim_tool_result.
VAULT_THRESHOLD = 6_000

# Redis TTL for vault entries (seconds). One work-session turn.
VAULT_TTL = 900

# Number of preview items included in the compact envelope.
VAULT_PREVIEW_ITEMS = 3

# Keys in a result dict that hold list payloads.
_LIST_KEYS = ("items", "results", "rows", "hits", "data")


def _list_key(data: dict) -> str | None:
    return next((k for k in _LIST_KEYS if isinstance(data.get(k), list)), None)


def should_vault(content_json: str) -> bool:
    """Return True when the serialised result exceeds VAULT_THRESHOLD."""
    return len(content_json) > VAULT_THRESHOLD


async def vault_store(session_id: str, result: dict) -> str:
    """Persist *result* in Redis and return the opaque vault_ref key."""
    from app.utils.redis_client import get_async_redis
    ref = f"vault:{session_id}:{uuid.uuid4().hex[:12]}"
    r = get_async_redis()
    await r.set(ref, json.dumps(result, ensure_ascii=False), ex=VAULT_TTL)
    logger.debug("vault_store ref=%s session=%s", ref, session_id)
    return ref


async def vault_get(ref: str, offset: int = 0, limit: int = 20) -> dict | None:
    """Retrieve a paginated slice of the vaulted result. Returns None if expired."""
    from app.utils.redis_client import get_async_redis
    r = get_async_redis()
    raw = await r.get(ref)
    if not raw:
        return None
    data = json.loads(raw)
    lk = _list_key(data)
    if lk:
        items: list = data[lk]
        page = items[offset:offset + limit]
        meta = {k: v for k, v in data.items() if k != lk}
        return {
            **meta,
            lk: page,
            "offset": offset,
            "limit": limit,
            "total_stored": len(items),
            "has_more": offset + limit < len(items),
        }
    return data


def make_vault_envelope(result: dict, vault_ref: str) -> dict:
    """Build the compact envelope that goes into conversation history.

    Includes a schema-revealing preview (first 3 items) so the model knows
    the data shape without seeing the full payload.
    """
    lk = _list_key(result)
    total: int = result.get("total") or (len(result[lk]) if lk else 0)
    # Copy all scalar/meta fields, drop the list payload
    envelope: dict = {
        k: v for k, v in result.items()
        if k not in _LIST_KEYS
    }
    envelope["vault_ref"] = vault_ref
    envelope["total"] = total
    if lk and result.get(lk):
        envelope[lk] = result[lk][:VAULT_PREVIEW_ITEMS]
    envelope["_vault_note"] = (
        f"[{total} записей. Для отображения используй workspace.* (рекомендуется). "
        f"Для постраничного чтения: vault action=get_page vault_ref='{vault_ref}'.]"
    )
    return envelope
