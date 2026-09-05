"""Unit tests for app.ai.thinking_params — the shared provider → HTTP-params
mapper for chain-of-thought (thinking) control — plus a regression test for
the gap it closes in OpenAICompatibleProvider.chat() (thinking was silently
dropped on the main AIRouter path for vllm/openrouter/openai/groq/xai/
dashscope/qwen/cerebras before this).
"""

from __future__ import annotations

import httpx
import pytest

from app.ai.schemas import AIRequest, AITask, ChatMessage, ProviderConfig, ProviderKind
from app.ai.thinking_params import (
    ANTHROPIC_DEFAULT_THINKING_BUDGET,
    ANTHROPIC_THINKING_BUDGET_TOKENS,
    effective_thinking_levels,
    thinking_request_params,
)

# ── effective_thinking_levels: automatic provider-class derivation ─────────


@pytest.mark.parametrize(
    "provider",
    [
        "anthropic",
        "openrouter",
        "openai",
        "groq",
        "xai",
        "dashscope",
        "qwen",
        "cerebras",
        "ollama_cloud",
    ],
)
def test_levels_auto_derived_for_guaranteed_providers(provider):
    # No curated_levels needed — these providers accept the level as a
    # documented, provider-level wire feature for ANY thinking-capable model.
    assert effective_thinking_levels(True, provider, []) == ["low", "medium", "high"]


@pytest.mark.parametrize(
    "provider", ["ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible"]
)
def test_levels_not_auto_derived_for_local_providers(provider):
    # Empirically verified 2026-08-17: Ollama accepts a string think level
    # without erroring but does not honor it for non-gpt-oss templates —
    # auto-deriving here would silently promise a control that does nothing.
    assert effective_thinking_levels(True, provider, []) == []


def test_curated_levels_always_win_over_derivation():
    # Explicit catalog curation (manual YAML, or the gpt-oss name-hint) is
    # never overridden by the provider-class auto-derivation, even when the
    # curated list differs from the guaranteed low/medium/high default.
    assert effective_thinking_levels(True, "anthropic", ["low"]) == ["low"]
    assert effective_thinking_levels(True, "ollama", ["low", "medium", "high"]) == [
        "low",
        "medium",
        "high",
    ]


def test_levels_require_thinking_supported():
    assert effective_thinking_levels(False, "anthropic", []) == []


# ── thinking_request_params: one case per provider branch ──────────────────


def test_ollama_off():
    assert thinking_request_params("ollama", False, None) == {
        "think": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_ollama_on_no_level():
    assert thinking_request_params("ollama", True, None) == {"think": True}


def test_ollama_on_with_level_sends_string():
    assert thinking_request_params("ollama", True, "high") == {"think": "high"}


@pytest.mark.parametrize("provider", ["llamacpp", "vllm"])
def test_llamacpp_vllm_level_has_no_effect(provider):
    # The Qwen3 chat template only exposes a binary switch — a level must not
    # leak into the payload as an unsupported key.
    assert thinking_request_params(provider, True, "high") == {
        "chat_template_kwargs": {"enable_thinking": True}
    }
    assert thinking_request_params(provider, False, None) == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_openrouter_off():
    assert thinking_request_params("openrouter", False, "high") == {"reasoning": {"enabled": False}}


def test_openrouter_on_with_and_without_level():
    assert thinking_request_params("openrouter", True, "low") == {
        "reasoning": {"enabled": True, "effort": "low"}
    }
    assert thinking_request_params("openrouter", True, None) == {"reasoning": {"enabled": True}}


@pytest.mark.parametrize(
    "provider", ["openai", "groq", "xai", "dashscope", "qwen", "cerebras", "ollama_cloud"]
)
def test_reasoning_effort_family(provider):
    assert thinking_request_params(provider, False, None) == {"reasoning_effort": "none"}
    assert thinking_request_params(provider, True, "low") == {"reasoning_effort": "low"}
    assert thinking_request_params(provider, True, None) == {"reasoning_effort": "medium"}


def test_unknown_provider_stays_silent():
    assert thinking_request_params("lmstudio", False, None) == {}
    assert thinking_request_params("lmstudio", True, "high") == {}


def test_anthropic_budget_table_is_exported_not_a_dict_key():
    # Anthropic's payload shape doesn't fit the flat-dict-merge pattern (it
    # also needs to raise max_tokens) — providers/anthropic_provider.py uses
    # the budget table directly rather than this function.
    assert thinking_request_params("anthropic", True, "high") == {}
    assert ANTHROPIC_THINKING_BUDGET_TOKENS == {"low": 1024, "medium": 4096, "high": 16384}
    assert ANTHROPIC_DEFAULT_THINKING_BUDGET == 2048


# ── regression: OpenAICompatibleProvider.chat() now applies thinking ───────


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}


@pytest.mark.asyncio
async def test_openai_compatible_chat_applies_reasoning_effort(monkeypatch):
    """Before the fix, request.thinking was never read here — this call would
    have sent no reasoning_effort/off-params at all regardless of the request.
    """
    from app.ai.providers.openai_compatible import OpenAICompatibleProvider

    captured: dict = {}

    async def fake_post(self, url, headers=None, json=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # ProviderConfig.kind carries the *real* provider identity (GROQ) even
    # though this generic class's own `kind` attribute defaults to
    # OPENAI_COMPATIBLE — the fix reads config.kind, not self.kind, for
    # exactly this reason (see _thinking_params in openai_compatible.py).
    config = ProviderConfig(kind=ProviderKind.GROQ, base_url="https://api.groq.com/openai/v1")
    provider = OpenAICompatibleProvider(config)

    request = AIRequest(
        task=AITask.CLASSIFICATION,
        messages=[ChatMessage(role="user", content="hi")],
        thinking=True,
        thinking_level="low",
    )
    await provider.chat(request, "llama-3.3-70b")

    assert captured["json"]["reasoning_effort"] == "low"


@pytest.mark.asyncio
async def test_openai_compatible_chat_stays_silent_when_thinking_unset(monkeypatch):
    """A caller that bypasses AIRouter's resolution (thinking still None) must
    not have an explicit "off" forced onto it — stay silent, matching the
    provider default, exactly like before this module existed.
    """
    from app.ai.providers.openai_compatible import OpenAICompatibleProvider

    captured: dict = {}

    async def fake_post(self, url, headers=None, json=None, **kwargs):
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    config = ProviderConfig(kind=ProviderKind.GROQ, base_url="https://api.groq.com/openai/v1")
    provider = OpenAICompatibleProvider(config)

    request = AIRequest(
        task=AITask.CLASSIFICATION,
        messages=[ChatMessage(role="user", content="hi")],
    )
    await provider.chat(request, "llama-3.3-70b")

    assert "reasoning_effort" not in captured["json"]
