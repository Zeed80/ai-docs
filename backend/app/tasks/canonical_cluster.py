"""Celery task — auto-cluster unconfirmed canonical items using embeddings."""

from __future__ import annotations

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(name="canonical.auto_cluster")
def auto_cluster_canonical_items() -> dict:
    """Suggest canonical item merges by grouping similar unconfirmed items."""
    return run_async(_run())


async def _run() -> dict:
    from sqlalchemy import select

    from app.core.chat_bus import chat_bus
    from app.db.models import CanonicalItem
    from app.db.session import _get_session_factory

    merged = 0

    async with _get_session_factory()() as db:
        result = await db.execute(
            select(CanonicalItem).where(CanonicalItem.is_confirmed.is_(False))
        )
        items = result.scalars().all()

        # Group by normalized name prefix (simple heuristic — embeddings-based
        # clustering would replace this when the embedding service is available)
        groups: dict[str, list[CanonicalItem]] = {}
        for item in items:
            key = _normalize_name(item.name)
            groups.setdefault(key, []).append(item)

        for key, group in groups.items():
            if len(group) < 2:
                continue
            # Keep first item, copy aliases from others
            primary = group[0]
            for duplicate in group[1:]:
                if duplicate.name not in (primary.aliases or []):
                    primary.aliases = (primary.aliases or []) + [duplicate.name]
                # Mark duplicates as confirmed to hide from suggestions
                duplicate.is_confirmed = True
            merged += len(group) - 1

        if merged > 0:
            await db.commit()
            await chat_bus.publish({
                "type": "notification",
                "level": "info",
                "title": "Канонический справочник",
                "message": f"Авто-кластеризация: объединено {merged} позиций.",
                "entity_type": "canonical",
            })

    logger.info("canonical_auto_cluster_done", merged=merged)
    return {"merged": merged}


def _normalize_name(name: str) -> str:
    """Normalize item name for clustering (lowercase, strip extra spaces)."""
    return " ".join(name.lower().split())
