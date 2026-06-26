"""Per-assignment (per-slot) reasoning toggle.

The same model can run with reasoning in one slot and without in another:
the override lives in task_routing.thinking / agent_config tri-state, and the
router resolves per-call → per-task → per-model.
"""

import pytest

from app.ai import task_routing as tr
from app.ai.task_routing import TaskRouting
from app.ai.schemas import AITask
from app.api.providers_api import (
    _SLOT_THINKING_AGENT_FIELDS,
    _SLOT_THINKING_TASKS,
    _apply_slot_thinking,
    _registry,
    _slot_thinking_state,
    _slot_supports_thinking,
)


@pytest.fixture
def routing_mem_store(monkeypatch):
    store: dict[str, dict] = {}
    monkeypatch.setattr(tr, "_redis_get", lambda: dict(store) if store else None)

    def _set(value):
        store.clear()
        store.update(value)

    monkeypatch.setattr(tr, "_redis_set", _set)
    return store


def test_thinking_field_tristate_default():
    r = TaskRouting(task="embedding")
    assert r.thinking is None  # defer to model default


def test_slot_supports_thinking():
    assert _slot_supports_thinking("agent_orchestrator")
    assert _slot_supports_thinking("structured_extraction")
    assert not _slot_supports_thinking("embedding")
    assert not _slot_supports_thinking("rerank")


def test_apply_slot_thinking_writes_task_routing(routing_mem_store):
    _apply_slot_thinking("structured_extraction", True)
    for tval in _SLOT_THINKING_TASKS["structured_extraction"]:
        assert tr.get_routing_for(AITask(tval)).thinking is True
    # Turning it off (force) is distinct from default (None).
    _apply_slot_thinking("structured_extraction", False)
    assert tr.get_routing_for(AITask.STRUCTURED_EXTRACTION).thinking is False


def test_apply_slot_thinking_agent_field_tristate(monkeypatch):
    captured = {}
    import app.ai.agent_config as ac

    def _update(patch):
        captured.update(patch.model_dump(exclude_unset=True))
        return ac._default_config()

    monkeypatch.setattr("app.api.providers_api.update_builtin_agent_config", _update, raising=False)
    # apply imports update_builtin_agent_config locally — patch at source too.
    monkeypatch.setattr(ac, "update_builtin_agent_config", _update)

    _apply_slot_thinking("agent_fast", True)
    assert captured["fast_disable_thinking"] is False  # reasoning ON → disable=False
    _apply_slot_thinking("agent_fast", None)
    assert captured["fast_disable_thinking"] is None    # default


def test_slot_thinking_state_reports_effective_source(routing_mem_store):
    registry = _registry()
    _apply_slot_thinking("structured_extraction", None)
    state = _slot_thinking_state("structured_extraction", registry, "qwen3_5_9b_ollama")
    assert state["thinking_supported_by_slot"] is True
    assert state["thinking_supported_by_model"] is True
    assert state["thinking_effective"] is False
    assert state["thinking_source"] == "model"

    _apply_slot_thinking("structured_extraction", True)
    state = _slot_thinking_state("structured_extraction", registry, "qwen3_5_9b_ollama")
    assert state["thinking_override"] is True
    assert state["thinking_effective"] is True
    assert state["thinking_source"] == "slot"


def test_slot_thinking_state_flags_unknown_disable_knob(routing_mem_store):
    from app.ai.schemas import ModelCapability, ModelStatus, Modality, ProviderKind

    registry = _registry()
    registry.models["lmstudio_thinker_test"] = ModelCapability(
        name="lmstudio_thinker_test",
        provider=ProviderKind.LMSTUDIO,
        provider_model="thinker",
        status=ModelStatus.PRODUCTION,
        modalities={Modality.TEXT},
        thinking_supported=True,
        thinking_enabled=False,
        local_only=True,
    )

    state = _slot_thinking_state("agent_fast", registry, "lmstudio_thinker_test")
    assert state["thinking_effective"] is False
    assert state["thinking_disable_supported"] is False
    assert state["thinking_warning"]


@pytest.mark.asyncio
async def test_router_resolves_per_task_thinking(routing_mem_store, monkeypatch):
    """router.run prefers task_routing.thinking over the model catalog default."""
    from app.ai.router import ai_router
    from app.ai.schemas import AIRequest, ChatMessage

    # Force reasoning ON for EMAIL_DRAFTING via the slot override.
    _apply_slot_thinking("agent_email", True)

    captured_thinking = {}

    async def _fake_dispatch(provider, request, model):
        captured_thinking["value"] = request.thinking
        from app.ai.schemas import AIResponse
        return AIResponse(
            text="ok", model=model.provider_model,
            task=request.task, provider=model.provider.value,
        )

    monkeypatch.setattr(ai_router, "_dispatch", _fake_dispatch)

    await ai_router.run(
        AIRequest(
            task=AITask.EMAIL_DRAFTING,
            messages=[ChatMessage(role="user", content="hi")],
        )
    )
    assert captured_thinking["value"] is True


def test_reasoning_disable_params_covers_ollama():
    """Ollama tool-call path must actually emit a thinking-off knob (regression:
    it returned {} so disabled-thinking never reached the model)."""
    from app.ai.agent_loop import _reasoning_disable_params

    ollama = _reasoning_disable_params("ollama")
    assert ollama.get("chat_template_kwargs") == {"enable_thinking": False}
    assert ollama.get("think") is False

    # llamacpp / vllm use the Qwen3 template kwarg.
    assert _reasoning_disable_params("llamacpp") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    assert _reasoning_disable_params("vllm") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }

    # Strict endpoints without a known knob stay empty (avoid 400s).
    assert _reasoning_disable_params("lmstudio") == {}
    assert _reasoning_disable_params("openai_compatible") == {}
