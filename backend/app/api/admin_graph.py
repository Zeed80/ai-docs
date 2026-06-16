"""Admin GUI for the background graph analytics (god nodes/clusters/surprising
connections) and the manual "rebuild graph" trigger.

Read side reads MemoryFact(kind="graph_insight") directly (structured, typed
by insight_type) rather than going through memory.search — the admin page
wants the full current snapshot, not a relevance-ranked subset.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.graph_analytics import GraphAnalyticsSettings, get_graph_analytics_settings, save_graph_analytics_settings
from app.auth.jwt import require_role
from app.auth.models import UserInfo, UserRole
from app.db.models import KnowledgeEdge, KnowledgeNode, MemoryFact
from app.db.session import get_db

logger = structlog.get_logger()

router = APIRouter(prefix="/api/admin/graph", tags=["admin-graph"])


class GraphInsightOut(BaseModel):
    id: uuid.UUID
    insight_type: str
    title: str
    summary: str
    confidence: float
    metadata: dict | None = None


class GraphStatsOut(BaseModel):
    nodes: int
    edges: int
    last_run_at: str | None = None
    insight_count: int


class RebuildQueuedOut(BaseModel):
    task_id: str
    status: str = "queued"


@router.get("/insights", response_model=list[GraphInsightOut])
async def list_graph_insights(
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> list[GraphInsightOut]:
    result = await db.execute(
        select(MemoryFact)
        .where(MemoryFact.kind == "graph_insight")
        .order_by(MemoryFact.confidence.desc(), MemoryFact.created_at.desc())
    )
    facts = result.scalars().all()
    return [
        GraphInsightOut(
            id=f.id,
            insight_type=(f.metadata_ or {}).get("insight_type", "unknown"),
            title=f.title,
            summary=f.summary,
            confidence=f.confidence,
            metadata=f.metadata_,
        )
        for f in facts
    ]


@router.get("/stats", response_model=GraphStatsOut)
async def graph_stats(
    db: AsyncSession = Depends(get_db),
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> GraphStatsOut:
    nodes = (await db.execute(select(func.count()).select_from(KnowledgeNode))).scalar_one()
    edges = (await db.execute(select(func.count()).select_from(KnowledgeEdge))).scalar_one()
    insight_count = (
        await db.execute(
            select(func.count()).select_from(MemoryFact).where(MemoryFact.kind == "graph_insight")
        )
    ).scalar_one()
    state = (
        await db.execute(
            select(MemoryFact).where(
                MemoryFact.kind == "graph_analytics_state",
                MemoryFact.title == "graph_analytics_state",
            )
        )
    ).scalar_one_or_none()
    last_run_at = (state.metadata_ or {}).get("last_run_at") if state else None
    return GraphStatsOut(nodes=nodes, edges=edges, last_run_at=last_run_at, insight_count=insight_count)


@router.get("/settings", response_model=GraphAnalyticsSettings)
async def get_settings(
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> GraphAnalyticsSettings:
    return get_graph_analytics_settings()


@router.post("/settings", response_model=GraphAnalyticsSettings)
async def update_settings(
    payload: GraphAnalyticsSettings,
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> GraphAnalyticsSettings:
    return save_graph_analytics_settings(payload)


@router.post("/rebuild", response_model=RebuildQueuedOut)
async def rebuild_graph(
    _user: UserInfo = Depends(require_role(UserRole.admin)),
) -> RebuildQueuedOut:
    """Backfill business-entity nodes/edges for every existing document, then
    force-recompute graph_insight facts. Runs as a background Celery task —
    can take a while on a large corpus."""
    from app.tasks.graph_memory import rebuild_business_graph

    task = rebuild_business_graph.delay()
    logger.info("graph_rebuild_queued", task_id=str(task.id))
    return RebuildQueuedOut(task_id=str(task.id))
