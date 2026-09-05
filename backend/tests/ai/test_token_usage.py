"""Б15: token usage capture in the Ollama streaming path and its aggregation
on AgentSession — the part of Б15 verifiable without a live database (the
budget-enforcement half, domain.work_orders.enforce_budgets, needs Postgres
and is covered separately in test_work_orders.py, unrunnable in this
sandbox — see AGENT_SYSTEM_REMEDIATION_PLAN.md Б15 for the honest caveat).
"""

from __future__ import annotations

import json

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig


def _config() -> BuiltinAgentConfig:
    return BuiltinAgentConfig(
        model="mock",
        backend_url="http://backend",
        ollama_url="http://ollama",
    )


class _FakeStreamResponse:
    def __init__(self, lines: list[dict]):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield json.dumps(line)


class _FakeStreamCtx:
    def __init__(self, lines: list[dict]):
        self._lines = lines

    async def __aenter__(self):
        return _FakeStreamResponse(self._lines)

    async def __aexit__(self, *a):
        return False


class FakeOllamaClient:
    def __init__(self, lines: list[dict], *a, **k):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None):  # noqa: A002
        return _FakeStreamCtx(self._lines)


@pytest.mark.asyncio
async def test_call_ollama_streaming_captures_usage_from_final_chunk(monkeypatch):
    lines = [
        {"message": {"role": "assistant", "content": "Hello"}, "done": False},
        {
            "message": {"role": "assistant", "content": " world"},
            "done": True,
            "prompt_eval_count": 42,
            "eval_count": 17,
        },
    ]
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *a, **k: FakeOllamaClient(lines))
    tokens: list[str] = []

    async def on_token(t: str) -> None:
        tokens.append(t)

    result = await agent_loop._call_ollama_streaming(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        system_prompt="",
        config=_config(),
        on_token=on_token,
    )

    assert result["content"] == "Hello world"
    assert result["_usage"] == {"input_tokens": 42, "output_tokens": 17}


@pytest.mark.asyncio
async def test_call_ollama_streaming_no_usage_key_when_ollama_omits_counts(monkeypatch):
    """Defensive: an Ollama response missing the count fields (older server,
    proxy stripping fields) must not fabricate a {0, 0} usage entry that would
    look like "measured and confirmed zero" downstream."""
    lines = [{"message": {"role": "assistant", "content": "hi"}, "done": True}]
    monkeypatch.setattr(agent_loop.httpx, "AsyncClient", lambda *a, **k: FakeOllamaClient(lines))

    async def on_token(t: str) -> None:
        pass

    result = await agent_loop._call_ollama_streaming(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        system_prompt="",
        config=_config(),
        on_token=on_token,
    )

    assert "_usage" not in result


def test_accumulate_usage_sums_across_calls(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_builtin_agent_config", lambda: _config())
    session = agent_loop.AgentSession(lambda event: None)

    assert session.total_tokens == {"input_tokens": 0, "output_tokens": 0}
    session._accumulate_usage({"_usage": {"input_tokens": 10, "output_tokens": 5}})
    session._accumulate_usage({"_usage": {"input_tokens": 3, "output_tokens": 2}})
    assert session.total_tokens == {"input_tokens": 13, "output_tokens": 7}

    # No "_usage" key (non-Ollama provider, or nothing captured) -> no-op.
    session._accumulate_usage({"content": "no usage here"})
    session._accumulate_usage(None)
    assert session.total_tokens == {"input_tokens": 13, "output_tokens": 7}
