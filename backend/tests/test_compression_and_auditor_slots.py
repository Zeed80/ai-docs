"""Сжатие контекста и аудитор ответов — обычные слоты назначения.

Обе настройки существовали и раньше, но задавались мимо каталога моделей:

* «модель для сжатия» вводилась свободным текстом в настройках агента. Имя
  никто не проверял, а провайдер вообще не хранился — запрос всегда уходил
  провайдеру worker'а. Написать туда `claude-sonnet-5` значило отправить
  облачное имя в локальную Ollama;
* аудитор не был представлен на экране вовсе и просто копировал модель
  агента, хотя в конфиге у него собственные поля и собственный выключатель
  облака.

Здесь проверяется, что через слот они настраиваются целиком — с провайдером,
и что разрешение облака доходит до того флага, который читает роутер.
"""

import pytest

from app.ai import agent_config as ac
from app.ai.agent_config import get_builtin_agent_config
from app.api import providers_api as p


@pytest.fixture(autouse=True)
def isolated_agent_config(monkeypatch, tmp_path):
    """Конфиг агента — в памяти теста, а не в файле и Redis работающего стека.

    Без этого тест назначения переписал бы боевую модель сжатия: data/ у
    стека примонтирован с хоста, а Redis тестов — та же база, что у него.
    """
    store: dict = {}
    monkeypatch.setattr(ac, "_redis_get_agent_config", lambda: dict(store) or None)
    monkeypatch.setattr(ac, "_redis_set_agent_config", lambda d: store.update(d))
    monkeypatch.setattr(ac, "_CONFIG_FILE", tmp_path / "agent_config.json")
    return store


@pytest.fixture(autouse=True)
def isolated_cloud_slots(monkeypatch):
    """То же для набора слотов с разрешённым облаком."""
    allowed: set[str] = set()

    class _FakeRedis:
        def smembers(self, _key):
            return set(allowed)

        def sadd(self, _key, member):
            allowed.add(member)

        def srem(self, _key, member):
            allowed.discard(member)

    monkeypatch.setattr("app.utils.redis_client.get_sync_redis", lambda: _FakeRedis())
    return allowed


@pytest.fixture
def registry():
    return p._registry()


def test_both_slots_are_declared(registry):
    ids = {slot for slot, *_ in p._SLOTS}
    assert {"agent_compression", "agent_auditor"} <= ids


@pytest.mark.parametrize("slot", ["agent_compression", "agent_auditor"])
def test_slot_reads_back_what_it_wrote(slot, registry):
    key = "qwen3_5_9b_ollama"
    p._apply_slot_assignment(slot, key, registry)
    assert p._slot_current_model(slot, registry) == key


def test_compression_stores_the_provider_too(registry):
    """Без провайдера сжатие уходило чужому узлу — это и был дефект."""
    p._apply_slot_assignment("agent_compression", "qwen3_5_9b_ollama", registry)
    cfg = get_builtin_agent_config()
    assert cfg.compression_provider == "ollama"
    assert cfg.compression_model == registry.models["qwen3_5_9b_ollama"].provider_model


def test_auditor_stores_the_provider_too(registry):
    p._apply_slot_assignment("agent_auditor", "qwen3_5_9b_ollama", registry)
    cfg = get_builtin_agent_config()
    assert cfg.auditor_provider == "ollama"


def test_unset_clears_compression_back_to_the_agent_model(registry):
    p._apply_slot_assignment("agent_compression", "qwen3_5_9b_ollama", registry)
    p._unset_slot("agent_compression", registry)
    cfg = get_builtin_agent_config()
    # Пусто = «использовать основную модель агента», как и было до слота.
    assert cfg.compression_model is None
    assert cfg.compression_provider is None


def test_cloud_permission_on_the_auditor_reaches_the_flag_the_router_reads():
    """`auditor_allow_cloud` — то, что проверяет orchestrator.semantic_audit.

    Разрешение только на слоте позволило бы ВЫБРАТЬ облачную модель, а вызов
    всё равно ушёл бы локально: молчаливое расхождение вместо настройки.
    """
    p._set_slot_cloud_allowed("agent_auditor", True)
    assert get_builtin_agent_config().auditor_allow_cloud is True

    p._set_slot_cloud_allowed("agent_auditor", False)
    assert get_builtin_agent_config().auditor_allow_cloud is False


def test_affected_lists_name_the_config_fields():
    assert p._slot_affected("agent_compression") == ["agent_config.compression_model"]
    assert p._slot_affected("agent_auditor") == ["agent_config.auditor_model"]
