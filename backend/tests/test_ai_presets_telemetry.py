"""Unit tests for hardware presets and usage telemetry (PR4)."""

import pytest

from app.ai import presets, task_routing as tr, telemetry
from app.ai.schemas import AITask


@pytest.fixture
def mem_routing(monkeypatch):
    store: dict[str, dict] = {}
    monkeypatch.setattr(tr, "_redis_get", lambda: dict(store) if store else None)

    def _set(value):
        store.clear()
        store.update(value)

    monkeypatch.setattr(tr, "_redis_set", _set)
    return store


# ── Presets ──────────────────────────────────────────────────────────────────


def test_presets_list_and_valid_keys():
    items = presets.list_presets()
    names = {p["name"] for p in items}
    assert {"rtx3090_balanced", "rtx3090_quality", "low_vram_12gb"} <= names

    # every model key referenced by a preset must exist in the catalog
    catalog = tr.known_model_keys()
    for name in names:
        p = presets.get_preset(name)
        for task, cfg in (p.get("routing") or {}).items():
            for key in cfg.get("models", []):
                assert key in catalog, f"{name}/{task}: unknown model {key}"


def test_apply_preset_writes_routing(mem_routing, monkeypatch):
    # Avoid touching real gpu_manager VRAM store.
    from app.ai import gpu_manager

    monkeypatch.setattr(gpu_manager, "_load_vram_limits", lambda: {})
    monkeypatch.setattr(gpu_manager, "save_vram_limits", lambda limits: None)

    result = presets.apply_preset("rtx3090_balanced")
    assert "engineering_reasoning" in result["applied"]
    assert result["skipped"] == []

    reasoning = tr.get_routing_for(AITask.ENGINEERING_REASONING)
    assert reasoning.models[0] == "qwen3_5_9b_ollama"


def test_apply_unknown_preset_raises():
    with pytest.raises(ValueError):
        presets.apply_preset("does_not_exist")


# ── Telemetry ──────────────────────────────────────────────────────────────────


class _FakePipe:
    def __init__(self, store):
        self.store = store
        self.ops = []

    def hincrby(self, key, field, n):
        self.store.setdefault(key, {})
        self.store[key][field] = self.store[key].get(field, 0) + n

    def lpush(self, key, val):
        self.store.setdefault(key, [])
        self.store[key].insert(0, val)

    def ltrim(self, key, a, b):
        self.store[key] = self.store.get(key, [])[a : b + 1]

    def execute(self):
        return None


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def pipeline(self):
        return _FakePipe(self.store)

    def hgetall(self, key):
        return self.store.get(key, {})

    def lrange(self, key, a, b):
        return self.store.get(key, [])[a : b + 1]

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


def test_telemetry_record_and_summary(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(telemetry, "_redis", lambda: fake)

    telemetry.record_call(task="invoice_ocr", model="gemma4_e4b_ollama",
                          provider="ollama", latency_ms=120, ok=True,
                          input_tokens=10, output_tokens=5)
    telemetry.record_call(task="invoice_ocr", model="gemma4_e4b_ollama",
                          provider="ollama", latency_ms=80, ok=False, error="boom")

    summary = telemetry.get_summary()
    assert summary["totals"]["calls"] == 2
    assert summary["totals"]["errors"] == 1
    row = next(r for r in summary["by_model"] if r["model"] == "gemma4_e4b_ollama")
    assert row["calls"] == 2
    assert row["avg_latency_ms"] == 100  # (120 + 80) / 2
    assert len(summary["recent"]) == 2

    telemetry.reset()
    assert telemetry.get_summary()["totals"]["calls"] == 0
