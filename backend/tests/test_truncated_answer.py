"""The answer must not end mid-sentence without anyone noticing.

Live W4 (Strata Swift): the reply stopped on «указывается в паспорте/» and
was saved as the final answer — max_tokens had cut it and finish_reason was
dropped by every streaming path. Now the reason is carried on the message
and a cut final answer gets one continuation, or an explicit note.
"""

from __future__ import annotations

import json

import pytest

from app.ai import agent_loop
from app.ai.agent_config import BuiltinAgentConfig
from app.ai.agent_loop import AgentSession

# ── finish_reason is read from the stream ────────────────────────────────────


class _Stream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    @property
    def status_code(self):
        return 200

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None, json=None):
        return _Stream(self._lines)


def _openai_chunks(text: str, finish: str | None) -> list[str]:
    chunks = [
        {"choices": [{"delta": {"content": part}, "finish_reason": None}]}
        for part in text.split(" ")
    ]
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    return [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["length", "stop"])
async def test_openai_stream_reports_finish_reason(monkeypatch, finish):
    monkeypatch.setattr(
        agent_loop.httpx, "AsyncClient", lambda *a, **k: _Client(_openai_chunks("масса 87", finish))
    )

    async def noop(_):
        pass

    config = BuiltinAgentConfig(model="m", provider="deepseek")
    monkeypatch.setattr(
        agent_loop,
        "_openai_compatible_provider_config",
        lambda provider, cfg: ("http://llm/v1", "", {}),
    )
    msg = await agent_loop._call_openai_streaming(
        [{"role": "user", "content": "масса?"}],
        [],
        "",
        config,
        noop,
        "deepseek",
        max_tokens=100,
    )
    assert msg["_finish_reason"] == finish
    assert "масса" in msg["content"]


@pytest.mark.asyncio
async def test_anthropic_stream_reports_max_tokens(monkeypatch):
    events = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "напор 32"},
        },
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
    ]
    monkeypatch.setattr(
        agent_loop.httpx,
        "AsyncClient",
        lambda *a, **k: _Client([f"data: {json.dumps(e)}" for e in events]),
    )

    async def noop(_):
        pass

    msg = await agent_loop._call_anthropic_streaming(
        [{"role": "user", "content": "напор?"}],
        [],
        "",
        BuiltinAgentConfig(model="claude", provider="anthropic"),
        noop,
        max_tokens=10,
        node=type("N", (), {"api_key": "k", "base_url": "http://anthropic", "extra": {}})(),
    )
    assert msg["_finish_reason"] == "max_tokens"


# ── the turn continues a cut answer ──────────────────────────────────────────


@pytest.fixture
def session():
    async def noop(_msg):
        pass

    s = AgentSession(noop)
    s._config = BuiltinAgentConfig(model="m", provider="ollama", ollama_url="http://ollama")
    s.messages = [{"role": "user", "content": "характеристики насоса"}]
    return s


@pytest.mark.asyncio
async def test_a_cut_answer_is_continued(monkeypatch, session):
    calls: list[list[dict]] = []

    async def fake_stream(messages, tools, system, config, on_token, **kw):
        calls.append(messages)
        await on_token("в паспорте изготовителя и в каталоге завода.")

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_stream)
    cut = {
        "role": "assistant",
        "content": "Обычно масса указывается в паспорте/",
        "_finish_reason": "length",
    }
    assert session._hit_token_limit(cut)
    full = await session._continue_truncated_answer(cut["content"], cut)
    assert (
        full == "Обычно масса указывается в паспорте/ в паспорте изготовителя и в каталоге завода."
    )
    # The continuation was one tool-less call with the cut answer in history.
    assert len(calls) == 1
    assert any("паспорте/" in str(m.get("content")) for m in calls[0])
    # The continuation request does not stay in the dialogue.
    assert session.messages[-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_a_failed_continuation_leaves_the_cut_visible(monkeypatch, session):
    async def fake_stream(*a, **kw):
        raise RuntimeError("model gone")

    monkeypatch.setattr(agent_loop, "_call_provider_streaming", fake_stream)
    cut = {
        "role": "assistant",
        "content": "Масса указывается в паспорте/",
        "_finish_reason": "length",
    }
    assert await session._continue_truncated_answer(cut["content"], cut) is None


def test_a_normal_answer_is_not_a_cut(session):
    assert not session._hit_token_limit({"_finish_reason": "stop"})
    assert not session._hit_token_limit({})
    assert session._hit_token_limit({"_finish_reason": "max_output_tokens"})
