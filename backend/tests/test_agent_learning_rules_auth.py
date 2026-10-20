"""The agent reads its learned rules from the backend as the service, not anonymously."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from app.ai import agent_loop


def test_learning_rules_request_carries_the_service_headers(monkeypatch):
    """Every turn got 401 and ran without its learned rules (live, 2026-10-20)."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        agent_loop.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(agent_loop, "internal_headers", lambda: {"X-API-Key": "svc"})
    session = agent_loop.AgentSession.__new__(agent_loop.AgentSession)
    session._config = SimpleNamespace(backend_url="http://backend:8000")
    session.messages = [{"role": "user", "content": "привет"}]

    asyncio.run(session._inject_learning_rules())

    assert seen and seen[0].url.path == "/api/technology/learning-rules"
    assert seen[0].headers.get("X-API-Key") == "svc"
