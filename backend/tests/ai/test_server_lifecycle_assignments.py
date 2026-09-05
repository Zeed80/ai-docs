"""A model server runs because something is assigned to it — or it does not run.

vLLM and llama.cpp pin their model in VRAM for the container's whole lifetime,
so a server nobody routes to is not idle capacity, it is a GPU nobody can use.
Measured on this box: the llama.cpp server held 22 of 24 GB while no assignment
referred to it, because `docker compose up` starts everything in the profile.
"""

from __future__ import annotations

import pytest

from app.ai import server_lifecycle


@pytest.fixture
def docker(monkeypatch):
    """A fake Docker socket: records actions, reports state."""
    state = {"running": {"llamacpp": True, "vllm": True}, "actions": []}

    async def fake_is_running(provider: str) -> bool:
        return bool(state["running"].get(provider))

    async def fake_action(provider: str, action: str) -> bool:
        state["actions"].append((provider, action))
        state["running"][provider] = action == "start"
        return True

    monkeypatch.setattr(server_lifecycle, "_docker_available", lambda: True)
    monkeypatch.setattr(server_lifecycle, "is_running", fake_is_running)
    monkeypatch.setattr(server_lifecycle, "_docker_action", fake_action)
    return state


def _assign(monkeypatch, *providers: str) -> None:
    monkeypatch.setattr(server_lifecycle, "assigned_providers", lambda: set(providers))


@pytest.mark.asyncio
async def test_a_server_nothing_is_assigned_to_is_stopped(monkeypatch, docker):
    _assign(monkeypatch, "ollama")  # everything routed to Ollama

    stopped = await server_lifecycle.stop_unassigned_servers()

    assert sorted(stopped) == ["llamacpp", "vllm"]
    assert sorted(docker["actions"]) == [("llamacpp", "stop"), ("vllm", "stop")]


@pytest.mark.asyncio
async def test_an_assigned_server_is_left_alone(monkeypatch, docker):
    _assign(monkeypatch, "ollama", "llamacpp")

    stopped = await server_lifecycle.stop_unassigned_servers()

    assert stopped == ["vllm"]
    assert ("llamacpp", "stop") not in docker["actions"]


@pytest.mark.asyncio
async def test_unreadable_assignments_stop_nothing(monkeypatch, docker):
    """A Redis hiccup must not read as "nothing is assigned" and reap a server
    the running system depends on."""

    def explode() -> set[str]:
        raise RuntimeError("redis down")

    monkeypatch.setattr(server_lifecycle, "assigned_providers", explode)

    assert await server_lifecycle.stop_unassigned_servers() == []
    assert docker["actions"] == []


@pytest.mark.asyncio
async def test_an_unassigned_server_is_never_started(monkeypatch, docker):
    """A stale fallback chain can address a server nobody assigned; starting it
    costs the whole GPU."""
    _assign(monkeypatch, "ollama")
    docker["running"]["llamacpp"] = False

    assert await server_lifecycle.ensure_running("llamacpp") is False
    assert docker["actions"] == []


@pytest.mark.asyncio
async def test_an_assigned_server_starts_on_demand(monkeypatch, docker):
    _assign(monkeypatch, "llamacpp")
    docker["running"]["llamacpp"] = False

    async def healthy(provider: str, timeout: float = 240.0) -> bool:
        return True

    monkeypatch.setattr(server_lifecycle, "_wait_healthy", healthy)

    assert await server_lifecycle.ensure_running("llamacpp") is True
    assert ("llamacpp", "start") in docker["actions"]


@pytest.mark.asyncio
async def test_the_idle_sweep_reaps_the_unassigned_first(monkeypatch, docker):
    """Unassigned goes regardless of the idle clock — it is not idle capacity,
    it is a server nothing routes to."""
    _assign(monkeypatch, "ollama")
    monkeypatch.setattr(server_lifecycle, "last_used", lambda provider: None)

    stopped = await server_lifecycle.stop_idle_servers()

    assert sorted(stopped) == ["llamacpp", "vllm"]


@pytest.mark.asyncio
async def test_unassigned_is_reaped_even_with_on_demand_switched_off(monkeypatch, docker):
    _assign(monkeypatch, "ollama")
    monkeypatch.setattr(server_lifecycle, "on_demand_enabled", lambda: False)

    assert sorted(await server_lifecycle.stop_idle_servers()) == ["llamacpp", "vllm"]


def test_only_primary_assignments_count(monkeypatch):
    """A fallback entry says "try this if the assigned model fails" — it is not
    a decision to keep a second model resident."""
    from app.ai.schemas import AITask

    class _Routing:
        def __init__(self, primary, models):
            self.primary = primary
            self.models = models

    from app.ai import task_routing

    monkeypatch.setattr(
        task_routing,
        "get_task_routing",
        lambda: {
            AITask.ENGINEERING_REASONING: _Routing(
                "qwen3_6_35b_apex_ollama",
                [
                    "qwen3_6_35b_apex_ollama",
                    "qwen3_vl_8b_vllm",
                ],
            ),
        },
    )
    # The fallback names a vLLM model; only the Ollama primary counts.
    assert server_lifecycle.assigned_providers() == {"ollama"}
