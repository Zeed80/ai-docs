"""Collections API — skills: collection.create, collection.add,
collection.summarize, collection.timeline"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import (
    AuditTimelineEvent,
    Collection,
    CollectionItem,
)
from app.domain.collections import (
    CollectionAddItem,
    CollectionCreate,
    CollectionOut,
    CollectionSummaryResponse,
    CollectionTimelineEvent,
    CollectionTimelineResponse,
)
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


# ── collection.create ──────────────────────────────────────────────────────


@router.post("", response_model=CollectionOut)
async def create_collection(
    payload: CollectionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: collection.create — Create a new collection."""
    coll = Collection(
        name=payload.name,
        description=payload.description,
        user_id="user",
    )
    db.add(coll)
    await db.commit()
    # Re-fetch with items loaded
    result = await db.execute(
        select(Collection).where(Collection.id == coll.id)
        .options(selectinload(Collection.items))
    )
    coll = result.scalar_one()
    logger.info("collection_created", id=str(coll.id), name=coll.name)
    return CollectionOut.model_validate(coll)


# ── collection.list ────────────────────────────────────────────────────────


@router.get("", response_model=list[CollectionOut])
async def list_collections(
    include_closed: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Skill: collection.list — List collections."""
    query = select(Collection).options(selectinload(Collection.items))
    if not include_closed:
        query = query.where(Collection.is_closed == False)
    query = query.order_by(Collection.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


# ── collection.get ─────────────────────────────────────────────────────────


@router.get("/{collection_id}", response_model=CollectionOut)
async def get_collection(
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: collection.get — Get collection with items."""
    result = await db.execute(
        select(Collection)
        .where(Collection.id == collection_id)
        .options(selectinload(Collection.items))
    )
    coll = result.scalar_one_or_none()
    if not coll:
        raise HTTPException(404, "Collection not found")
    return coll


# ── collection.add ─────────────────────────────────────────────────────────


@router.post("/{collection_id}/items", response_model=CollectionOut)
async def add_item(
    collection_id: uuid.UUID,
    payload: CollectionAddItem,
    db: AsyncSession = Depends(get_db),
):
    """Skill: collection.add — Add item to collection."""
    result = await db.execute(
        select(Collection)
        .where(Collection.id == collection_id)
        .options(selectinload(Collection.items))
    )
    coll = result.scalar_one_or_none()
    if not coll:
        raise HTTPException(404, "Collection not found")
    if coll.is_closed:
        raise HTTPException(400, "Collection is closed")

    # Check duplicate
    existing = await db.execute(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.entity_type == payload.entity_type,
            CollectionItem.entity_id == payload.entity_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(400, "Item already in collection")

    item = CollectionItem(
        collection_id=collection_id,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        note=payload.note,
    )
    db.add(item)

    await add_timeline_event(
        db, entity_type="collection", entity_id=collection_id,
        event_type="item_added",
        summary=f"Added {payload.entity_type} {payload.entity_id}",
        actor="user",
    )
    await db.commit()
    await db.refresh(coll)
    return coll


# ── collection.remove item ─────────────────────────────────────────────────


@router.delete("/{collection_id}/items/{item_id}")
async def remove_item(
    collection_id: uuid.UUID,
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Remove item from collection."""
    result = await db.execute(
        select(CollectionItem).where(
            CollectionItem.id == item_id,
            CollectionItem.collection_id == collection_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, "Item not found")
    await db.delete(item)
    await db.commit()
    return {"status": "removed"}


# ── collection.close ───────────────────────────────────────────────────────


@router.post("/{collection_id}/close", response_model=CollectionOut)
async def close_collection(
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Close a collection and generate summary."""
    result = await db.execute(
        select(Collection)
        .where(Collection.id == collection_id)
        .options(selectinload(Collection.items))
    )
    coll = result.scalar_one_or_none()
    if not coll:
        raise HTTPException(404, "Collection not found")

    coll.is_closed = True
    coll.closed_at = datetime.now(timezone.utc)

    # Auto-generate closure summary
    entity_counts: dict[str, int] = {}
    for item in coll.items:
        entity_counts[item.entity_type] = entity_counts.get(item.entity_type, 0) + 1

    parts = [f"{count} {etype}" for etype, count in entity_counts.items()]
    coll.closure_summary = f"Коллекция закрыта. Содержит: {', '.join(parts)}." if parts else "Пустая коллекция закрыта."

    await add_timeline_event(
        db, entity_type="collection", entity_id=collection_id,
        event_type="closed", summary=coll.closure_summary, actor="user",
    )
    await db.commit()
    await db.refresh(coll)
    return coll


# ── collection.summarize ───────────────────────────────────────────────────


@router.post("/{collection_id}/summarize", response_model=CollectionSummaryResponse)
async def summarize_collection(
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: collection.summarize — Summarize collection contents."""
    result = await db.execute(
        select(Collection)
        .where(Collection.id == collection_id)
        .options(selectinload(Collection.items))
    )
    coll = result.scalar_one_or_none()
    if not coll:
        raise HTTPException(404, "Collection not found")

    entity_types: dict[str, int] = {}
    for item in coll.items:
        entity_types[item.entity_type] = entity_types.get(item.entity_type, 0) + 1

    parts = []
    for etype, count in entity_types.items():
        label = {"document": "документов", "invoice": "счетов", "email": "писем", "supplier": "поставщиков"}.get(etype, etype)
        parts.append(f"{count} {label}")

    summary = f"Коллекция «{coll.name}»: {', '.join(parts)}." if parts else f"Коллекция «{coll.name}» пуста."

    return CollectionSummaryResponse(
        collection_id=coll.id,
        summary=summary,
        item_count=len(coll.items),
        entity_types=entity_types,
    )


# ── collection.timeline ───────────────────────────────────────────────────


@router.get("/{collection_id}/timeline", response_model=CollectionTimelineResponse)
async def collection_timeline(
    collection_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: collection.timeline — Get timeline of events for items in collection."""
    result = await db.execute(
        select(Collection)
        .where(Collection.id == collection_id)
        .options(selectinload(Collection.items))
    )
    coll = result.scalar_one_or_none()
    if not coll:
        raise HTTPException(404, "Collection not found")

    events: list[CollectionTimelineEvent] = []

    # Get timeline events for all items in collection
    for item in coll.items:
        evts = await db.execute(
            select(AuditTimelineEvent)
            .where(
                AuditTimelineEvent.entity_type == item.entity_type,
                AuditTimelineEvent.entity_id == item.entity_id,
            )
            .order_by(AuditTimelineEvent.timestamp.desc())
            .limit(10)
        )
        for evt in evts.scalars().all():
            events.append(CollectionTimelineEvent(
                timestamp=evt.timestamp.isoformat(),
                event_type=evt.event_type,
                entity_type=evt.entity_type,
                entity_id=str(evt.entity_id),
                summary=evt.summary,
            ))

    # Also include collection-level events
    coll_evts = await db.execute(
        select(AuditTimelineEvent)
        .where(
            AuditTimelineEvent.entity_type == "collection",
            AuditTimelineEvent.entity_id == collection_id,
        )
        .order_by(AuditTimelineEvent.timestamp.desc())
    )
    for evt in coll_evts.scalars().all():
        events.append(CollectionTimelineEvent(
            timestamp=evt.timestamp.isoformat(),
            event_type=evt.event_type,
            entity_type="collection",
            entity_id=str(collection_id),
            summary=evt.summary,
        ))

    # Sort by time desc
    events.sort(key=lambda e: e.timestamp, reverse=True)

    return CollectionTimelineResponse(
        collection_id=collection_id,
        events=events,
        total=len(events),
    )
