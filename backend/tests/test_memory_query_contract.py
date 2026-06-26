from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api import memory
from app.domain.graph import MemoryQueryRequest


@pytest.mark.asyncio
async def test_memory_query_returns_compact_evidence_pack(monkeypatch):
    hit_id = uuid.uuid4()
    doc_id = uuid.uuid4()

    async def fake_explain(payload, db):
        return SimpleNamespace(
            query=payload.query,
            hits=[
                SimpleNamespace(
                    kind="fact",
                    id=hit_id,
                    title="Поставщик АКМЕ",
                    summary="АКМЕ поставляет крепеж",
                    source="chat",
                    score=0.8,
                    source_document_id=doc_id,
                    evidence=None,
                )
            ],
            nodes=[{"id": "node-1"}],
            edges=[{"id": "edge-1"}],
        )

    monkeypatch.setattr(memory, "explain_memory", fake_explain)

    result = await memory.query_memory(
        MemoryQueryRequest(
            query="АКМЕ",
            scope="session",
            session_id="session-1",
            include_graph=False,
        ),
        db=None,
    )

    assert result.query == "АКМЕ"
    assert len(result.evidence_pack) == 1
    assert result.evidence_pack[0].id == hit_id
    assert result.evidence_pack[0].source_document_id == doc_id
    assert result.graph_nodes == []
    assert result.graph_edges == []
