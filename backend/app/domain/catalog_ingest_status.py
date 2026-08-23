"""Progress of background supplier-catalog ingestion, per tool supplier.

Parsing a real PDF catalog is minutes of local-LLM work per fragment, so the
attach call queues the work and returns immediately. The agent (and the user
asking "как там каталоги?") still needs to see what happened to each source —
that is what this store holds: one record per source URL, overwritten as the
worker progresses (queued → running → attached/empty/error).

Redis-backed with an in-memory fallback, mirroring app.domain.workspace: a
Redis hiccup must degrade to "status unknown after restart", never break the
ingestion itself.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "catalog_ingest:"
_TTL_SECONDS = 7 * 86_400
_FALLBACK: dict[str, dict[str, dict[str, Any]]] = {}


def _redis():
    try:
        from app.utils.redis_client import get_sync_redis

        return get_sync_redis()
    except Exception:  # noqa: BLE001
        return None


def record_source_status(
    supplier_id: str,
    url: str,
    *,
    status: str,
    title: str | None = None,
    entries_created: int = 0,
    entries_conflicted: int = 0,
    message: str = "",
) -> None:
    """Upsert one source's state. Never raises — status is not the payload."""
    record = {
        "url": url,
        "title": title,
        "status": status,
        "entries_created": entries_created,
        "entries_conflicted": entries_conflicted,
        "message": message,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    key = f"{_KEY_PREFIX}{supplier_id}"
    client = _redis()
    if client is not None:
        try:
            client.hset(key, url, json.dumps(record, ensure_ascii=False))
            client.expire(key, _TTL_SECONDS)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog_ingest_status_write_failed: %s", exc)
    _FALLBACK.setdefault(supplier_id, {})[url] = record


def list_source_statuses(supplier_id: str) -> list[dict[str, Any]]:
    """All known source states for a supplier, newest update first."""
    key = f"{_KEY_PREFIX}{supplier_id}"
    records: list[dict[str, Any]] = []
    client = _redis()
    if client is not None:
        try:
            for raw in (client.hgetall(key) or {}).values():
                try:
                    records.append(json.loads(raw))
                except Exception:  # noqa: BLE001
                    continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("catalog_ingest_status_read_failed: %s", exc)
    if not records:
        records = list(_FALLBACK.get(supplier_id, {}).values())
    return sorted(records, key=lambda r: str(r.get("updated_at") or ""), reverse=True)


def forget_source_status(supplier_id: str, url: str) -> None:
    """Забыть запись об одном источнике.

    Вызывается при удалении каталога: иначе запись «загружено, 312 позиций»
    живёт ещё неделю (TTL) и выглядит актуальной, хотя позиций уже нет —
    агент, спросив ingest_status, отчитается о том, чего в системе не осталось.
    """
    key = f"{_KEY_PREFIX}{supplier_id}"
    client = _redis()
    if client is not None:
        try:
            client.hdel(key, url)
            return
        except Exception as exc:  # noqa: BLE001 — статус не данные
            logger.warning("catalog_ingest_status_forget_failed: %s", exc)
    _FALLBACK.get(supplier_id, {}).pop(url, None)


def clear_source_statuses(supplier_id: str) -> None:
    client = _redis()
    if client is not None:
        try:
            client.delete(f"{_KEY_PREFIX}{supplier_id}")
        except Exception:  # noqa: BLE001
            pass
    _FALLBACK.pop(supplier_id, None)
