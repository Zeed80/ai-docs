"""Unit tests for the provider instance resolver, secret box, and thinking flow.

These run offline: Redis is monkeypatched so resolution falls back to YAML/env
defaults, and the AIRouter is exercised with a stub provider.
"""

from __future__ import annotations

import pytest

from app.ai import provider_registry as pr
from app.ai import secret_box
from app.ai.router import AIRouter
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AITask,
    AIUsage,
    ProviderKind,
)


# ── secret_box ──────────────────────────────────────────────────────────────


def test_secret_box_roundtrip():
    ct = secret_box.encrypt("sk-ant-secret-123456")
    assert ct != "sk-ant-secret-123456"
    assert secret_box.decrypt(ct) == "sk-ant-secret-123456"


def test_secret_box_empty_and_invalid():
    assert secret_box.encrypt("") == ""
    assert secret_box.decrypt("") == ""
    assert secret_box.decrypt("not-a-valid-token") == ""  # fail-closed


def test_secret_box_mask_hides_secret():
    masked = secret_box.mask("sk-ant-secret-123456")
    assert masked.endswith("3456")
    assert "secret" not in masked


# ── provider_registry resolution ────────────────────────────────────────────


@pytest.fixture
def no_redis(monkeypatch):
    """Force the empty-cache path so resolution uses YAML/env defaults."""
    monkeypatch.setattr(pr, "_redis_get_instances", lambda: [])
    pr._availability_cache.clear()


def test_default_instance_local_and_cloud(no_redis):
    ollama = pr.list_instances(ProviderKind.OLLAMA)
    assert len(ollama) == 1
    assert ollama[0].is_local is True
    assert ollama[0].base_url.startswith("http")

    anthropic = pr.select_instance(ProviderKind.ANTHROPIC)
    assert anthropic.is_local is False
    assert "anthropic" in anthropic.base_url


def test_select_instance_prefers_named_pin(monkeypatch):
    rows = [
        {"id": "1", "kind": "ollama", "name": "node-a", "base_url": "http://a:11434", "enabled": True, "is_local": True},
        {"id": "2", "kind": "ollama", "name": "node-b", "base_url": "http://b:11434", "enabled": True, "is_local": True},
    ]
    monkeypatch.setattr(pr, "_redis_get_instances", lambda: rows)
    pr._availability_cache.clear()
    sel = pr.select_instance(ProviderKind.OLLAMA, "qwen3.5:9b", preferred_instance="node-b")
    assert sel.base_url == "http://b:11434"


def test_select_instance_routes_to_node_hosting_model(monkeypatch):
    rows = [
        {"id": "1", "kind": "ollama", "name": "node-a", "base_url": "http://a:11434", "enabled": True, "is_local": True},
        {"id": "2", "kind": "ollama", "name": "node-b", "base_url": "http://b:11434", "enabled": True, "is_local": True},
    ]
    monkeypatch.setattr(pr, "_redis_get_instances", lambda: rows)
    pr._availability_cache.clear()

    def fake_models(node):
        return {"qwen3.5:9b"} if "b:" in node.base_url else set()

    monkeypatch.setattr(pr, "_models_on_node", fake_models)
    sel = pr.select_instance(ProviderKind.OLLAMA, "qwen3.5:9b")
    assert sel.base_url == "http://b:11434"


def test_disabled_instances_excluded(monkeypatch):
    rows = [
        {"id": "1", "kind": "vllm", "name": "v-a", "base_url": "http://a:8000", "enabled": False, "is_local": True},
    ]
    monkeypatch.setattr(pr, "_redis_get_instances", lambda: rows)
    pr._availability_cache.clear()
    # Falls back to YAML default since the only row is disabled.
    insts = pr.list_instances(ProviderKind.VLLM)
    assert all(i.base_url != "http://a:8000" for i in insts)


# ── thinking flow through the router ────────────────────────────────────────


class _StubProvider:
    def __init__(self):
        self.last_request: AIRequest | None = None

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        self.last_request = request
        return AIResponse(
            task=request.task,
            provider=ProviderKind.OLLAMA,
            model=model,
            text="ok",
            usage=AIUsage(input_tokens=1, output_tokens=1),
        )

    async def vision(self, request, model):  # pragma: no cover
        return await self.chat(request, model)


@pytest.fixture
def router_with_stub(monkeypatch):
    r = AIRouter()
    stub = _StubProvider()
    r.providers = {kind: stub for kind in r.registry.providers}
    return r, stub


def _route_to(monkeypatch, model_key):
    from app.ai import task_routing as tr

    routing = tr.TaskRouting(
        task=AITask.CLASSIFICATION.value, models=[model_key],
        profile="balanced", local_only=True, allow_cloud=False,
    )
    monkeypatch.setattr(tr, "get_routing_for", lambda t, _r=routing: _r)


@pytest.mark.asyncio
async def test_thinking_defaults_to_model_catalog(monkeypatch, router_with_stub):
    r, stub = router_with_stub
    # gemma4_e4b has thinking_supported=False → effective thinking must be False
    _route_to(monkeypatch, "gemma4_e4b_ollama")
    await r.run(AIRequest(task=AITask.CLASSIFICATION, prompt="hi", confidential=True))
    assert stub.last_request.thinking is False


@pytest.mark.asyncio
async def test_thinking_per_call_override_wins(monkeypatch, router_with_stub):
    r, stub = router_with_stub
    _route_to(monkeypatch, "gemma4_e4b_ollama")
    await r.run(AIRequest(task=AITask.CLASSIFICATION, prompt="hi", confidential=True, thinking=True))
    assert stub.last_request.thinking is True
