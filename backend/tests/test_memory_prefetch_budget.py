"""The agent's per-turn memory prefetch must fit its wait.

Reranking 30 candidates on the CPU Ollama node took ~14 s and the turn waits
12 s: every turn waited the full 12 s and got no memory (live, 2026-10-20).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.ai import memory_manager
from app.ai.memory_manager import MemoryManager
from app.api import memory as memory_api
from app.domain.graph import MemoryQueryRequest, MemorySearchRequest


def test_agent_prefetch_asks_for_memory_without_reranking(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"evidence_pack": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        memory_manager.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    asyncio.run(MemoryManager(base_url="http://backend:8000").prefetch("счета Ромашки"))
    assert bodies and bodies[0]["rerank"] is False


def test_rerank_is_on_by_default_for_other_callers():
    assert MemoryQueryRequest(query="x").rerank is True
    assert MemorySearchRequest(query="x").rerank is True


def test_search_reranks_only_when_asked():
    """search_memory gates the reranker on the request's switch."""
    import inspect

    source = inspect.getsource(memory_api.search_memory)
    assert "if payload.rerank and _reranker_model_key():" in source
