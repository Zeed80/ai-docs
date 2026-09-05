"""Здоровье слота: доступность, вызовы, ошибки, задержка и расход.

Все три источника существовали, но лежали на разных вкладках: доступность
модели — в каталоге, вызовы и задержка — в телеметрии, мёртвые звенья
цепочки — в отдельной панели. Человек, назначивший модель, не мог узнать,
работает ли она, не уходя с экрана.
"""

import pytest

from app.api import providers_api as p


@pytest.fixture
def telemetry_rows(monkeypatch):
    """Телеметрия хранит строку на КАЖДУЮ пару (задача, модель)."""
    rows = {
        "by_model": [
            # Текущая модель слота.
            {
                "task": "embedding",
                "model": "qwen3_embedding_4b_ollama",
                "calls": 1000,
                "errors": 7,
                "avg_latency_ms": 1044,
                "tokens_in": 500_000,
                "tokens_out": 0,
            },
            # Давно снятая модель той же задачи — её статистика не должна
            # приписываться слоту.
            {
                "task": "embedding",
                "model": "local_embedding_ollama",
                "calls": 846,
                "errors": 846,
                "avg_latency_ms": 2249,
                "tokens_in": 0,
                "tokens_out": 0,
            },
        ]
    }
    monkeypatch.setattr("app.ai.telemetry.get_summary", lambda: rows)
    return rows


@pytest.mark.asyncio
async def test_health_counts_only_the_model_the_slot_uses_now(telemetry_rows, monkeypatch):
    """Схлопывание строк по одной задаче приписывало слоту чужие цифры: на
    стенде embedding показывал 100% ошибок от модели, которой в каталоге уже
    нет, вместо 0.7% у назначенной."""
    monkeypatch.setattr(p, "_slot_current_model", lambda slot, reg: "qwen3_embedding_4b_ollama")
    monkeypatch.setattr("app.ai.provider_registry.catalog_availability", lambda models: {})

    rows = await p.slots_health()
    embedding = next(r for r in rows if r.slot == "embedding")

    assert embedding.calls == 1000
    assert embedding.errors == 7
    assert embedding.error_rate == pytest.approx(0.007)
    assert embedding.avg_latency_ms == 1044


@pytest.mark.asyncio
async def test_every_slot_is_reported_even_without_traffic(monkeypatch):
    monkeypatch.setattr("app.ai.telemetry.get_summary", lambda: {"by_model": []})
    monkeypatch.setattr("app.ai.provider_registry.catalog_availability", lambda models: {})

    rows = await p.slots_health()
    assert len(rows) == len(p._SLOTS)
    assert all(r.calls == 0 and r.error_rate == 0.0 for r in rows)


@pytest.mark.asyncio
async def test_unknown_price_is_not_reported_as_free(monkeypatch):
    """cost_per_1k_* в каталоге не заполнена почти нигде. Ноль вместо «не
    знаем» превратил бы неизвестный расход в уверенное «ничего не потрачено»."""
    monkeypatch.setattr("app.ai.telemetry.get_summary", lambda: {"by_model": []})
    monkeypatch.setattr("app.ai.provider_registry.catalog_availability", lambda models: {})

    rows = await p.slots_health()
    for r in rows:
        if not r.priced:
            assert r.cost_usd is None


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_take_the_whole_report_down(monkeypatch):
    def boom():
        raise RuntimeError("redis is down")

    monkeypatch.setattr("app.ai.telemetry.get_summary", boom)
    monkeypatch.setattr("app.ai.provider_registry.catalog_availability", lambda models: {})

    rows = await p.slots_health()
    assert len(rows) == len(p._SLOTS)
