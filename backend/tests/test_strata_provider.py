"""Strata provider: GPU ownership guard and the settings file on its volume."""

from __future__ import annotations

import json
import struct

import pytest

from app.ai import gpu_runtime
from app.ai.providers import strata_manager
from app.ai.schemas import ProviderKind
from app.ai.thinking_params import thinking_request_params


@pytest.fixture
def owner(monkeypatch):
    state: dict[str, str | None] = {"owner": "ollama"}
    monkeypatch.setattr(gpu_runtime, "current_owner", lambda: state["owner"])
    monkeypatch.setattr(gpu_runtime, "is_gpu_ollama", lambda url: url != "http://ollama-cpu:11434")
    return state


def test_strata_refused_while_gpu_is_ollamas(owner):
    with pytest.raises(gpu_runtime.GpuRuntimeUnavailable, match="Strata выключена"):
        gpu_runtime.check_call("strata", "http://strata:8080")
    gpu_runtime.check_call("ollama", "http://host-gateway:11434")


def test_gpu_ollama_refused_while_gpu_is_stratas(owner):
    owner["owner"] = "strata"
    gpu_runtime.check_call("strata", "http://strata:8080")
    with pytest.raises(gpu_runtime.GpuRuntimeUnavailable, match="отдана Strata"):
        gpu_runtime.check_call("ollama", "http://host-gateway:11434")
    # The CPU node (embeddings/reranking) is not the GPU's: untouched by the switch.
    gpu_runtime.check_call("ollama", "http://ollama-cpu:11434")


def test_unknown_owner_refuses_nothing(owner):
    owner["owner"] = None
    gpu_runtime.check_call("strata", None)
    gpu_runtime.check_call("ollama", None)


def test_other_providers_never_guarded(owner):
    owner["owner"] = "strata"
    gpu_runtime.check_call("anthropic", None)
    gpu_runtime.check_call("llamacpp", None)


def test_guard_error_is_a_runtime_error_the_router_skips():
    # AIRouter catches (KeyError, ValueError, RuntimeError) per candidate.
    assert issubclass(gpu_runtime.GpuRuntimeUnavailable, RuntimeError)


def test_desired_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    assert strata_manager.read_desired() == {
        "model": "IQ3_S",
        "context": 65536,
        "vision": True,
        "reinstall_pending": False,
        "install_only": False,
    }
    strata_manager.write_desired(model="IQ2_XS", context=32768, vision=False, reinstall=True)
    text = (tmp_path / "aiw.env").read_text()
    assert "MODEL=IQ2_XS" in text and "VISION=no" in text and "REINSTALL=1" in text
    assert strata_manager.read_desired() == {
        "model": "IQ2_XS",
        "context": 32768,
        "vision": False,
        "reinstall_pending": True,
        "install_only": False,
    }
    strata_manager.write_desired(
        model="IQ3_S", context=65536, vision=True, reinstall=False, install_only=True
    )
    assert strata_manager.read_desired()["install_only"] is True


def test_desired_rejects_unknown_quant_and_context(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    with pytest.raises(strata_manager.StrataError):
        strata_manager.write_desired(model="Q8_0", context=65536, vision=True, reinstall=False)
    with pytest.raises(strata_manager.StrataError):
        strata_manager.write_desired(model="IQ3_S", context=1000, vision=True, reinstall=False)


def test_installed_quants_are_the_finished_setups(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "strata-iq3_s.json").write_text(json.dumps({"args": []}))
    # A download in progress has model files but no run config yet.
    (tmp_path / "models" / "iq2_xs").mkdir(parents=True)
    assert strata_manager.installed_quants() == ["IQ3_S"]


def test_quant_that_does_not_fit_the_disk_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(strata_manager, "disk_free_gb", lambda: 50.0)
    with pytest.raises(strata_manager.StrataError, match="Мало места"):
        strata_manager.check_quant_fits_disk("IQ3_S")


def test_docker_log_stream_is_demultiplexed():
    def frame(kind: int, text: str) -> bytes:
        data = text.encode()
        return struct.pack(">BxxxI", kind, len(data)) + data

    raw = frame(1, "Setting up iq3_s\n") + frame(2, "downloading 40%\n")
    assert strata_manager._demux_docker_logs(raw) == "Setting up iq3_s\ndownloading 40%\n"


@pytest.mark.parametrize(
    ("state", "health", "logs", "phase"),
    [
        (None, None, [], "not_created"),
        ("exited", None, [], "stopped"),
        ("running", None, ["Setting up iq3_s: downloading the model"], "installing"),
        ("running", None, ["[strata] loading experts"], "loading"),
        ("running", {"loaded": True}, [], "ready"),
        ("running", {"loaded": False}, [], "unloaded"),
    ],
)
def test_phase(state, health, logs, phase):
    assert strata_manager._phase(state, health, logs) == phase


def test_install_only_run_is_never_serving():
    assert strata_manager._phase("running", None, ["[strata] loading"], True) == "installing"
    assert strata_manager._phase("exited", None, [], True) == "stopped"


def test_strata_is_a_local_reasoning_effort_provider():
    from app.ai.provider_registry import _LOCAL_KINDS

    assert ProviderKind.STRATA in _LOCAL_KINDS
    assert thinking_request_params("strata", False, None) == {"reasoning_effort": "none"}
    assert thinking_request_params("strata", True, "high") == {"reasoning_effort": "high"}


def test_catalog_has_the_strata_model():
    from app.ai.model_registry import ModelRegistry

    reg = ModelRegistry.from_yaml("app/ai/config/model_registry.yaml")
    cap = reg.models["strata_qwen3_8_flash_next"]
    assert cap.provider == ProviderKind.STRATA
    assert "vision" in cap.modalities and cap.local_only
    assert ProviderKind.STRATA in reg.providers


def test_restore_reaches_slots_that_share_a_chain(monkeypatch):
    """ocr_large is the 2nd element of the chain whose head is ocr_fast.

    Putting ocr_fast back re-exposes Strata in ocr_large; the restore must
    notice and put it back too — live, a one-pass restore left it on Strata.
    """
    import asyncio
    from types import SimpleNamespace

    from app.api import providers_api as pa
    from app.api import strata_api

    strata = strata_api.STRATA_MODEL_KEY
    chain = [strata, strata]  # invoice_ocr after the switch: [ocr_fast, ocr_large]
    state = {"agent_orchestrator": strata, "embedding": "emb"}

    def current(slot, _registry):
        if slot == "ocr_fast":
            return chain[0]
        if slot == "ocr_large":
            return chain[1] if chain[1] != chain[0] else None
        return state.get(slot)

    async def apply(_db, _user, target):
        for slot, model in target.items():
            if slot == "ocr_fast":
                chain[:] = [model, *[m for m in chain if m != model]][:2]
            elif slot == "ocr_large":
                chain[1] = model
            else:
                state[slot] = model
        return "rev"

    revision = SimpleNamespace(
        diff=[
            {"slot": "ocr_fast", "old_model": "q9", "new_model": strata},
            {"slot": "ocr_large", "old_model": "q27", "new_model": strata},
            {"slot": "agent_orchestrator", "old_model": "tc27", "new_model": strata},
        ]
    )

    async def get_revision(_db, _rid):
        return revision

    monkeypatch.setattr(pa, "_registry", lambda: None)
    monkeypatch.setattr(pa, "_slot_current_model", current)
    monkeypatch.setattr(strata_api, "_apply_slots", apply)
    monkeypatch.setattr(strata_api.model_runtime_store, "get_assignment_revision", get_revision)
    monkeypatch.setattr(strata_manager, "_get_switch_revision", lambda: "r1")
    monkeypatch.setattr(strata_manager, "set_switch_revision", lambda _rid: None)

    restored = asyncio.run(strata_api._restore_slots(None, None))
    assert chain == ["q9", "q27"]
    assert state["agent_orchestrator"] == "tc27"
    assert set(restored) == {"ocr_fast", "ocr_large", "agent_orchestrator"}


def test_ocr_large_assignment_is_persisted(monkeypatch):
    """ocr_large lives in the invoice_ocr chain; that chain must reach Postgres.

    Its affected label is not a task value, so nothing was written and the
    hydrate right after the apply restored the old fallback.
    """
    import asyncio

    from app.ai import model_runtime_store
    from app.api import providers_api as pa

    written: list[str] = []

    async def persist(_db, *, task, routing):
        written.append(task)

    monkeypatch.setattr(model_runtime_store, "persist_task_routing", persist)
    asyncio.run(pa._persist_slot_durable(None, "ocr_large"))
    assert written == ["invoice_ocr"]


def test_idle_unload_keys_land_in_every_installed_config(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_DATA_DIR", str(tmp_path))
    (tmp_path / "config").mkdir()
    cfg_path = tmp_path / "config" / "strata-iq3_s.json"
    cfg_path.write_text(json.dumps({"args": ["--pack", "/data/packs/iq3_s"], "port": 8080}))

    # Default: unload after 5 min, ask ComfyUI to free VRAM before a reload.
    assert strata_manager.apply_runtime_keys() == ["IQ3_S"]
    cfg = json.loads(cfg_path.read_text())
    assert cfg["idle_unload_s"] == 300
    assert cfg["min_free_vram_mib"] == 12000
    assert cfg["before_load"][-1].endswith("/free")
    assert cfg["args"] == ["--pack", "/data/packs/iq3_s"]  # the rest is untouched
    assert strata_manager.apply_runtime_keys() == []  # idempotent

    strata_manager.write_runtime(idle_unload_s=0, free_comfyui=True)
    strata_manager.apply_runtime_keys()
    cfg = json.loads(cfg_path.read_text())
    assert "idle_unload_s" not in cfg and "before_load" not in cfg

    with pytest.raises(strata_manager.StrataError):
        strata_manager.write_runtime(idle_unload_s=7, free_comfyui=False)


def test_strata_is_unloaded_before_a_comfyui_run(monkeypatch):
    from app.ai import gpu_lock

    calls: list[str] = []
    monkeypatch.setattr(gpu_lock, "unload_ollama", lambda: calls.append("ollama") or 0)
    monkeypatch.setattr(gpu_lock, "unload_strata", lambda: calls.append("strata") or True)
    gpu_lock.unload_llm_servers()
    assert calls == ["ollama", "strata"]


def test_unload_strata_skips_a_model_that_is_not_loaded(monkeypatch):
    import io

    from app.ai import gpu_lock

    posted: list[str] = []

    def urlopen(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/health"):
            return io.BytesIO(b'{"loaded": false}')
        posted.append(url)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(gpu_lock.urllib.request, "urlopen", urlopen)
    assert gpu_lock.unload_strata() is False
    assert posted == []


def test_slotless_tasks_move_to_strata_and_come_back(monkeypatch):
    """engineering_reasoning (the work-order verifier) has no slot.

    Live: with the GPU on Strata the verifier still called GPU-Ollama directly
    and loaded a 35B model beside Strata. The switch must move such tasks and
    the restore must put back exactly their chains.
    """
    import asyncio

    from app.ai import model_runtime_store
    from app.ai.schemas import AITask
    from app.ai.task_routing import TaskRouting
    from app.api import providers_api as pa
    from app.api import strata_api

    strata = strata_api.STRATA_MODEL_KEY
    chains = {
        AITask.ENGINEERING_REASONING: ["apex_ollama", "q9_ollama"],
        AITask.EMBEDDING: ["emb_ollama"],
        AITask.STRUCTURED_EXTRACTION: ["q9_ollama"],
    }

    def routing_for(task):
        return TaskRouting(task=task.value, models=chains.get(task, []))

    def save(task, routing):
        chains[task] = list(routing.models)
        return routing

    class Cap:
        provider = ProviderKind.OLLAMA
        provider_model = "m"
        preferred_instance = None

    class Db:
        async def commit(self):
            return None

    async def noop(*_a, **_k):
        return None

    saved_tasks: dict = {}
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", routing_for)
    monkeypatch.setattr("app.ai.task_routing.save_task_routing", save)
    monkeypatch.setattr(
        "app.ai.task_routing.get_task_routing", lambda: {t: routing_for(t) for t in chains}
    )
    monkeypatch.setattr(
        pa,
        "_registry",
        lambda: type(
            "R", (), {"models": {"apex_ollama": Cap(), "emb_ollama": Cap(), "q9_ollama": Cap()}}
        )(),
    )
    monkeypatch.setattr(strata_api, "_on_gpu_ollama", lambda cap: True)
    monkeypatch.setattr(model_runtime_store, "persist_task_routing", noop)
    monkeypatch.setattr(model_runtime_store, "hydrate_runtime_cache", noop)
    monkeypatch.setattr(strata_manager, "get_switch_tasks", lambda: saved_tasks)
    monkeypatch.setattr(
        strata_manager,
        "set_switch_tasks",
        lambda v: saved_tasks.update(v) or (saved_tasks.clear() if not v else None),
    )

    plan = {p.slot: p for p in strata_api._task_plan(vision=True)}
    # Slot-covered (structured_extraction) and vector tasks are not listed.
    assert set(plan) == {"task:engineering_reasoning"}
    assert plan["task:engineering_reasoning"].move

    saved = asyncio.run(strata_api._move_tasks(Db(), ["engineering_reasoning"]))
    assert chains[AITask.ENGINEERING_REASONING] == [strata, "apex_ollama", "q9_ollama"]
    saved_tasks.update(saved)

    restored = asyncio.run(strata_api._restore_tasks(Db()))
    assert restored == ["task:engineering_reasoning"]
    assert chains[AITask.ENGINEERING_REASONING] == ["apex_ollama", "q9_ollama"]
    assert saved_tasks == {}


def test_direct_ollama_helpers_refuse_while_strata_owns_the_gpu(monkeypatch):
    import asyncio

    from app.ai import ollama_client

    monkeypatch.setattr(gpu_runtime, "current_owner", lambda: "strata")
    with pytest.raises(gpu_runtime.GpuRuntimeUnavailable):
        asyncio.run(ollama_client.generate("x", model="q9"))
    with pytest.raises(gpu_runtime.GpuRuntimeUnavailable):
        asyncio.run(ollama_client.generate_json("x", model="q9", provider="ollama"))


def test_same_model_on_the_cpu_node_is_used_while_strata_owns_the_gpu(monkeypatch):
    """qwen3-embedding:8b sits on both nodes; the GPU node came first and was refused."""
    from app.ai import provider_registry as pr

    gpu = pr.ResolvedProvider(
        kind=ProviderKind.OLLAMA,
        base_url="http://host-gateway:11434",
        is_local=True,
        name="ollama (default)",
    )
    cpu = pr.ResolvedProvider(
        kind=ProviderKind.OLLAMA,
        base_url="http://ollama-cpu:11434",
        is_local=True,
        name="ollama-cpu",
    )
    monkeypatch.setattr(pr, "list_instances", lambda kind: [gpu, cpu])
    monkeypatch.setattr(pr, "_models_on_node", lambda node: {"qwen3-embedding:8b"})
    monkeypatch.setattr(gpu_runtime, "is_gpu_ollama", lambda url: url == gpu.base_url)

    monkeypatch.setattr(gpu_runtime, "current_owner", lambda: "ollama")
    assert pr.select_instance(ProviderKind.OLLAMA, "qwen3-embedding:8b") is gpu
    monkeypatch.setattr(gpu_runtime, "current_owner", lambda: "strata")
    assert pr.select_instance(ProviderKind.OLLAMA, "qwen3-embedding:8b") is cpu
