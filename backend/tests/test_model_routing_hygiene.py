"""Цепочки моделей не должны накапливать мусор.

`_set_primary` ставил выбранную модель в голову и сохранял прежний хвост
ЦЕЛИКОМ, а экран назначения показывает только голову. Поэтому после перехода
gemma4 → qwen3.8 ссылки на gemma4 остались в цепочках у 16 задач из 19:
невидимо, пока голова жива, и 404 на каждой попытке фолбэка, как только она
недоступна.

Главное здесь — не сама чистка, а её осторожность: молчащий узел значит
«неизвестно», а не «модели нет».
"""

from app.ai.provider_registry import Availability
from app.ai.schemas import ProviderKind


class _Node:
    def __init__(self, base_url: str, kind=ProviderKind.OLLAMA):
        self.base_url = base_url
        self.kind = kind


def _patch(monkeypatch, nodes, models_by_node):
    import app.ai.provider_registry as pr

    monkeypatch.setattr(pr, "list_instances", lambda kind: nodes)
    monkeypatch.setattr(
        pr, "_models_on_node", lambda node: models_by_node.get(node.base_url, set())
    )


def test_model_present_on_any_node_is_available(monkeypatch):
    from app.ai.provider_registry import model_availability

    _patch(
        monkeypatch,
        [_Node("http://gpu"), _Node("http://cpu")],
        {"http://gpu": {"qwen3.8:27b"}, "http://cpu": {"qwen3-embedding:4b"}},
    )
    assert model_availability(ProviderKind.OLLAMA, "qwen3.8:27b") is Availability.AVAILABLE


def test_model_absent_from_every_answering_node_is_missing(monkeypatch):
    from app.ai.provider_registry import model_availability

    _patch(
        monkeypatch,
        [_Node("http://gpu")],
        {"http://gpu": {"qwen3.8:27b"}},
    )
    assert model_availability(ProviderKind.OLLAMA, "gemma4:e4b") is Availability.MISSING


def test_a_silent_node_means_unknown_not_missing(monkeypatch):
    """Самое важное свойство. Если узел не ответил, мы НЕ знаем, есть ли там
    модель. Считать это отсутствием — значит стирать рабочую настройку из-за
    одной сетевой заминки."""
    from app.ai.provider_registry import model_availability

    _patch(monkeypatch, [_Node("http://gpu")], {})  # узел молчит
    assert model_availability(ProviderKind.OLLAMA, "qwen3.8:27b") is Availability.UNKNOWN


def test_no_nodes_configured_is_unknown(monkeypatch):
    from app.ai.provider_registry import model_availability

    _patch(monkeypatch, [], {})
    assert model_availability(ProviderKind.OLLAMA, "qwen3.8:27b") is Availability.UNKNOWN


def test_cloud_providers_are_never_called_missing(monkeypatch):
    """Перечислить модели облачного шлюза дёшево нельзя — значит и судить
    об их отсутствии нельзя."""
    from app.ai.provider_registry import model_availability

    assert model_availability(ProviderKind.ANTHROPIC, "claude-sonnet-4-6") is Availability.UNKNOWN


def test_prune_drops_only_what_is_certainly_gone(monkeypatch):
    from app.ai import assignment_groups as ag

    class _Cap:
        def __init__(self, provider, provider_model):
            self.provider = provider
            self.provider_model = provider_model

    catalog = {
        "alive": _Cap(ProviderKind.OLLAMA, "qwen3.8:27b"),
        "gone": _Cap(ProviderKind.OLLAMA, "gemma4:e4b"),
        "cloudy": _Cap(ProviderKind.ANTHROPIC, "claude-sonnet-4-6"),
    }

    class _Reg:
        models = catalog

    monkeypatch.setattr(
        "app.ai.model_registry.ModelRegistry.from_yaml",
        classmethod(lambda cls, path: _Reg()),
    )
    import app.ai.provider_registry as pr

    monkeypatch.setattr(pr, "list_instances", lambda kind: [_Node("http://gpu")])
    monkeypatch.setattr(pr, "_models_on_node", lambda node: {"qwen3.8:27b"})

    kept, dropped = ag.prune_dead_keys(["alive", "gone", "cloudy", "never-existed"])
    assert kept == ["alive", "cloudy"]  # облако не трогаем
    assert dropped == ["gone", "never-existed"]


def test_prune_keeps_everything_when_the_catalog_is_unreadable(monkeypatch):
    """Без каталога судить не о чем — молча вычистить цепочку было бы худшим
    из возможных поведений."""
    from app.ai import assignment_groups as ag

    def _boom(cls, path):
        raise RuntimeError("нет файла")

    monkeypatch.setattr("app.ai.model_registry.ModelRegistry.from_yaml", classmethod(_boom))
    kept, dropped = ag.prune_dead_keys(["a", "b"])
    assert kept == ["a", "b"]
    assert dropped == []


async def test_prune_is_durable_not_just_cached(client, monkeypatch):
    """Redis — только кэш: назначения лежат в Postgres и при старте
    восстанавливаются оттуда ПОВЕРХ Redis.

    Первая версия чистки писала лишь в Redis, поэтому жила до первого
    рестарта и молча откатывалась — на живом стенде так и вышло: 25 убранных
    звеньев вернулись все до одного. Тест держит именно это свойство.
    """
    import app.api.providers_api as api

    persisted: list[str] = []
    hydrated: list[bool] = []

    async def _persist(db, *, task, routing):
        persisted.append(task)

    async def _hydrate(db):
        hydrated.append(True)

    monkeypatch.setattr(api.model_runtime_store, "persist_task_routing", _persist)
    monkeypatch.setattr(api.model_runtime_store, "hydrate_runtime_cache", _hydrate)

    resp = await client.post("/api/providers/routing-health/prune")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    if body["total"]:
        # Каждая изменённая задача обязана попасть в durable-хранилище.
        assert set(persisted) == set(body["pruned"]), (
            "изменение в Redis без записи в Postgres не переживёт рестарт"
        )
        assert hydrated, "после записи нужно перечитать кэш"
    else:
        assert not persisted


def test_a_configured_but_unusable_verifier_holds_the_document(monkeypatch, tmp_path):
    """Настроенная, но не отработавшая проверка — это НЕ пройденная проверка.

    `verify_model_1` месяцами указывал на llama.cpp-модель, контейнера которой
    нет. Каждая проверка падала, писала варнинг — и документ всё равно уходил
    в approved. Страховочная сетка исчезала молча, а именно так она и исчезает:
    модель удалили, провайдер не подняли.
    """
    import inspect

    from app.tasks import extraction

    src = inspect.getsource(extraction.auto_verify_document)
    empty_branch = src.split('logger.warning("auto_verify_model_empty"')[1]
    assert "_hold_for_review" in empty_branch.split("# ── Approve")[0], (
        "документ уходит в approved, хотя настроенная проверка не отработала"
    )
