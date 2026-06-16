"""Background graph analytics — "build once, query cheaply" over KnowledgeNode/Edge.

Loads the whole knowledge graph into networkx once per run, computes:
  - "god nodes" (highest-degree nodes — VIP suppliers/invoices worth watching)
  - clusters (greedy modularity communities — groups with a similar pattern)
  - surprising connections (rare edges crossing entity-type domains)

Results are stored as MemoryFact(kind="graph_insight") so the agent answers
relational "what's most connected/problematic" questions from a cache via
memory.search, instead of recomputing a live graph traversal per turn.

Dirty-flag invalidation: a run is skipped unless MAX(updated_at) across
KnowledgeNode/KnowledgeEdge has advanced past the last recorded run — the
graph is only "photographed" when it actually changed.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import networkx as nx
import structlog
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KnowledgeEdge, KnowledgeNode, MemoryFact

logger = structlog.get_logger()

_STATE_KIND = "graph_analytics_state"
_STATE_TITLE = "graph_analytics_state"
_INSIGHT_KIND = "graph_insight"

_SETTINGS_REDIS_KEY = "graph_analytics_settings"
_DEFAULT_INTERVAL_SECONDS = 86_400


class GraphAnalyticsSettings(BaseModel):
    """Runtime-tunable cadence for the background graph analytics task.

    Stored in Redis (not env/import-time) so it can change from the admin
    GUI without a container restart — the celery-beat tick (every 30 min,
    see app.tasks.celery_app) stays fixed, but the task self-throttles
    against this interval, the same dirty-flag pattern already used for
    "skip if the graph itself hasn't changed".
    """

    enabled: bool = True
    interval_seconds: int = Field(default=_DEFAULT_INTERVAL_SECONDS, ge=0)


def _redis_get_settings() -> dict | None:
    try:
        from app.utils.redis_client import get_sync_redis

        raw = get_sync_redis().get(_SETTINGS_REDIS_KEY)
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("graph_analytics_settings_read_failed", error=str(exc))
        return None


def _redis_set_settings(value: dict) -> None:
    from app.utils.redis_client import get_sync_redis

    get_sync_redis().set(_SETTINGS_REDIS_KEY, json.dumps(value))


def get_graph_analytics_settings() -> GraphAnalyticsSettings:
    raw = _redis_get_settings()
    return GraphAnalyticsSettings(**raw) if raw else GraphAnalyticsSettings()


def save_graph_analytics_settings(settings: GraphAnalyticsSettings) -> GraphAnalyticsSettings:
    _redis_set_settings(settings.model_dump())
    return settings

_GOD_NODE_TOP_N = 8
_GOD_NODE_MIN_DEGREE = 2
_CLUSTER_MIN_SIZE = 3
_CLUSTER_TOP_N = 6
_SURPRISING_MAX_DOMAIN_EDGE_COUNT = 2  # edge_type+domain-pair seen this rarely → "surprising"
_SURPRISING_TOP_N = 8


async def _max_graph_updated_at(db: AsyncSession) -> datetime | None:
    node_max = (await db.execute(select(func.max(KnowledgeNode.updated_at)))).scalar()
    edge_max = (await db.execute(select(func.max(KnowledgeEdge.updated_at)))).scalar()
    candidates = [v for v in (node_max, edge_max) if v is not None]
    return max(candidates) if candidates else None


async def _load_run_state(db: AsyncSession) -> dict[str, Any] | None:
    result = await db.execute(
        select(MemoryFact).where(MemoryFact.kind == _STATE_KIND, MemoryFact.title == _STATE_TITLE)
    )
    fact = result.scalar_one_or_none()
    return fact.metadata_ if fact else None


async def _save_run_state(db: AsyncSession, *, max_updated_at: datetime | None) -> None:
    result = await db.execute(
        select(MemoryFact).where(MemoryFact.kind == _STATE_KIND, MemoryFact.title == _STATE_TITLE)
    )
    fact = result.scalar_one_or_none()
    payload = {
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "max_graph_updated_at": max_updated_at.isoformat() if max_updated_at else None,
    }
    if fact is None:
        fact = MemoryFact(
            scope="project",
            kind=_STATE_KIND,
            title=_STATE_TITLE,
            summary="Внутреннее состояние фоновой графовой аналитики (не для чтения агентом).",
            source="graph_analytics",
            confidence=1.0,
            pinned=False,
            metadata_=payload,
        )
        db.add(fact)
    else:
        fact.metadata_ = payload


async def _load_graph(db: AsyncSession) -> nx.Graph:
    nodes = (await db.execute(select(KnowledgeNode))).scalars().all()
    edges = (await db.execute(select(KnowledgeEdge))).scalars().all()

    graph = nx.Graph()
    for node in nodes:
        domain = node.entity_type or node.node_type
        graph.add_node(str(node.id), title=node.title, domain=domain)
    for edge in edges:
        s, t = str(edge.source_node_id), str(edge.target_node_id)
        if s not in graph or t not in graph:
            continue
        graph.add_edge(s, t, edge_type=edge.edge_type)
    return graph


def _god_nodes(graph: nx.Graph) -> list[dict[str, Any]]:
    ranked = sorted(graph.degree(), key=lambda item: item[1], reverse=True)
    out = []
    for node_id, degree in ranked:
        if degree < _GOD_NODE_MIN_DEGREE:
            break
        out.append({"node_id": node_id, "title": graph.nodes[node_id]["title"], "degree": degree})
        if len(out) >= _GOD_NODE_TOP_N:
            break
    return out


def _clusters(graph: nx.Graph) -> list[list[str]]:
    if graph.number_of_edges() == 0:
        return []
    try:
        communities = nx.algorithms.community.greedy_modularity_communities(graph)
    except Exception as exc:
        logger.warning("graph_analytics_clustering_failed", error=str(exc))
        return []
    sized = [c for c in communities if len(c) >= _CLUSTER_MIN_SIZE]
    sized.sort(key=len, reverse=True)
    return [
        [graph.nodes[node_id]["title"] for node_id in community]
        for community in sized[:_CLUSTER_TOP_N]
    ]


def _surprising_connections(graph: nx.Graph) -> list[dict[str, Any]]:
    """Rare edges that cross entity-type domains — e.g. supplier <-> drawing."""
    cross_domain_edges: list[tuple[str, str, str, str]] = []
    for u, v, data in graph.edges(data=True):
        domain_u = graph.nodes[u].get("domain") or "?"
        domain_v = graph.nodes[v].get("domain") or "?"
        if domain_u == domain_v:
            continue
        pair = tuple(sorted((domain_u, domain_v)))
        cross_domain_edges.append((u, v, data.get("edge_type", "?"), f"{pair[0]}↔{pair[1]}"))

    if not cross_domain_edges:
        return []

    domain_pair_counts = Counter(item[3] for item in cross_domain_edges)
    rare = [
        item for item in cross_domain_edges
        if domain_pair_counts[item[3]] <= _SURPRISING_MAX_DOMAIN_EDGE_COUNT
    ]
    out = []
    for u, v, edge_type, domain_pair in rare[:_SURPRISING_TOP_N]:
        out.append({
            "source_title": graph.nodes[u]["title"],
            "target_title": graph.nodes[v]["title"],
            "edge_type": edge_type,
            "domain_pair": domain_pair,
        })
    return out


async def _replace_insights(db: AsyncSession, facts: list[MemoryFact]) -> None:
    await db.execute(delete(MemoryFact).where(MemoryFact.kind == _INSIGHT_KIND))
    for fact in facts:
        db.add(fact)


def _god_nodes_fact(god_nodes: list[dict[str, Any]]) -> MemoryFact | None:
    if not god_nodes:
        return None
    lines = [f"- {n['title']} (связей: {n['degree']})" for n in god_nodes]
    return MemoryFact(
        scope="project",
        kind=_INSIGHT_KIND,
        title="Самые связанные узлы графа памяти",
        summary="Узлы с наибольшим числом связей — кандидаты на приоритетное внимание:\n" + "\n".join(lines),
        source="graph_analytics",
        confidence=0.8,
        pinned=False,
        metadata_={"insight_type": "god_nodes", "nodes": god_nodes},
    )


def _cluster_facts(clusters: list[list[str]]) -> list[MemoryFact]:
    facts = []
    for idx, titles in enumerate(clusters, start=1):
        facts.append(MemoryFact(
            scope="project",
            kind=_INSIGHT_KIND,
            title=f"Кластер графа памяти #{idx}",
            summary=f"Группа взаимосвязанных узлов ({len(titles)}): " + ", ".join(titles),
            source="graph_analytics",
            confidence=0.7,
            pinned=False,
            metadata_={"insight_type": "cluster", "titles": titles},
        ))
    return facts


def _surprising_facts(surprising: list[dict[str, Any]]) -> list[MemoryFact]:
    facts = []
    for item in surprising:
        facts.append(MemoryFact(
            scope="project",
            kind=_INSIGHT_KIND,
            title=f"Неожиданная связь: {item['source_title']} ↔ {item['target_title']}",
            summary=(
                f"Редкая связь между доменами {item['domain_pair']} "
                f"(тип ребра: {item['edge_type']}) — обычно эти типы узлов не пересекаются."
            ),
            source="graph_analytics",
            confidence=0.6,
            pinned=False,
            metadata_={"insight_type": "surprising_connection", **item},
        ))
    return facts


async def run_graph_analytics_async(db: AsyncSession, *, force: bool = False) -> dict[str, Any]:
    """Recompute and store graph_insight facts unless throttled or unchanged.

    force=True (manual "rebuild" trigger) bypasses both the enabled/interval
    settings and the dirty-flag check — it always recomputes.
    """
    max_updated_at = await _max_graph_updated_at(db)
    state = await _load_run_state(db) if not force else None
    if not force:
        settings = get_graph_analytics_settings()
        if not settings.enabled:
            return {"skipped": True, "reason": "disabled"}

        last_run_at = state.get("last_run_at") if state else None
        if last_run_at:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run_at)).total_seconds()
            if elapsed < settings.interval_seconds:
                return {"skipped": True, "reason": "interval_not_elapsed"}

        prior = state.get("max_graph_updated_at") if state else None
        if prior and max_updated_at and datetime.fromisoformat(prior) >= max_updated_at:
            return {"skipped": True, "reason": "graph_unchanged"}
        if prior is None and max_updated_at is None:
            return {"skipped": True, "reason": "graph_empty"}

    graph = await _load_graph(db)
    god_nodes = _god_nodes(graph)
    clusters = _clusters(graph)
    surprising = _surprising_connections(graph)

    facts = []
    god_fact = _god_nodes_fact(god_nodes)
    if god_fact:
        facts.append(god_fact)
    facts.extend(_cluster_facts(clusters))
    facts.extend(_surprising_facts(surprising))

    await _replace_insights(db, facts)
    await _save_run_state(db, max_updated_at=max_updated_at)

    logger.info(
        "graph_analytics_run",
        nodes=graph.number_of_nodes(),
        edges=graph.number_of_edges(),
        god_nodes=len(god_nodes),
        clusters=len(clusters),
        surprising=len(surprising),
    )
    return {
        "skipped": False,
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "god_nodes": len(god_nodes),
        "clusters": len(clusters),
        "surprising": len(surprising),
    }
