from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app.api import agent


def test_retired_websocket_returns_410_before_model_start(monkeypatch) -> None:
    def forbidden_model_start(*args, **kwargs):
        raise AssertionError("retired WebSocket must not construct AgentOrchestrator")

    # Patch the historical constructor at its source. The retired endpoint no
    # longer imports it, so a successful denial proves the request never enters
    # the former model lifecycle.
    monkeypatch.setattr(
        "app.ai.orchestrator.AgentOrchestrator",
        forbidden_model_start,
    )
    app = FastAPI()
    app.include_router(agent.router)

    with TestClient(app) as client:
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect("/ws/chat"):
                pytest.fail("retired endpoint accepted the WebSocket")

    assert denied.value.status_code == 410
    assert denied.value.headers["deprecation"] == "true"
    assert denied.value.json() == {
        "code": "agent_ws_retired",
        "detail": (
            "WebSocket chat lifecycle retired. Use POST /api/agent/chat-runs and the "
            "durable run, event, checkpoint, resume, and WorkOrder cancel endpoints."
        ),
        "replacement": "/api/agent/chat-runs",
    }


def test_full_application_exposes_only_retired_websocket_boundary() -> None:
    from app.main import app

    client = TestClient(app)
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect("/ws/chat"):
            pytest.fail("full application accepted the retired WebSocket")

    assert denied.value.status_code == 410
    assert denied.value.json()["code"] == "agent_ws_retired"


@pytest.mark.asyncio
async def test_retired_websocket_closes_before_accept_without_denial_extension() -> None:
    ws = Mock()
    ws.client = None

    async def deny(_response):
        raise RuntimeError("extension unavailable")

    closed: list[tuple[int, str]] = []

    async def close(*, code: int, reason: str):
        closed.append((code, reason))

    ws.send_denial_response = deny
    ws.close = close

    await agent.chat_ws(ws)

    assert closed == [(1008, agent._FALLBACK_CLOSE_REASON)]
    assert len(agent._FALLBACK_CLOSE_REASON.encode("utf-8")) <= 123
    assert not hasattr(ws, "accept") or not ws.accept.called
