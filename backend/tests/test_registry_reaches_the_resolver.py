"""Назначенная модель должна доходить до исполнения без перезапуска.

`ai_router` — модульный синглтон, и его каталог читался единственный раз в
__init__. Модель, подтянутая через /refresh-models или найденная в
/live-models, немедленно появлялась в GUI и назначалась на слот, но резолвер
её не видел до перезапуска контейнера. Со стороны человека это выглядело как
«назначил и ничего не изменилось», без единого сообщения.
"""

from app.ai.router import AIRouter


def test_reload_registry_picks_up_a_model_added_after_startup(monkeypatch):
    router = AIRouter()
    before = len(router.registry.models)
    assert before > 0

    # Модель появилась в оверлее уже после старта процесса — ровно то, что
    # делает /refresh-models для облачного провайдера.
    from app.ai.model_registry import ModelRegistry

    real_from_yaml = ModelRegistry.from_yaml
    added_key = "test_model_added_at_runtime"

    def from_yaml_with_extra(path):
        reg = real_from_yaml(path)
        some = next(iter(reg.models.values()))
        reg.models[added_key] = some.model_copy(update={"name": added_key})
        return reg

    monkeypatch.setattr(ModelRegistry, "from_yaml", staticmethod(from_yaml_with_extra))

    assert added_key not in router.registry.models
    total = router.reload_registry()

    assert added_key in router.registry.models
    assert total == before + 1


def test_reload_does_not_undo_injected_providers():
    """Подставленные провайдеры — шов для тестов и встраивания.

    Если перезагрузка каталога втихую заменит их настоящими, тест начнёт
    ходить в живую модель и проверять не то, что заявлено.
    """
    sentinel = object()
    router = AIRouter()
    kind = next(iter(router.registry.providers))
    router.use_providers({kind: sentinel})  # type: ignore[dict-item]

    router.reload_registry()

    assert router.providers[kind] is sentinel


def test_defaults_cache_can_be_invalidated():
    """Кэш YAML-дефолтов маршрутизации не сбрасывался никогда — правка
    model_registry.yaml требовала перезапуска, ничем себя не обнаруживая."""
    from app.ai import task_routing

    task_routing._registry_defaults()
    assert task_routing._defaults_cache is not None

    task_routing.invalidate_defaults_cache()
    assert task_routing._defaults_cache is None

    # И снова заполняется по требованию.
    assert task_routing._registry_defaults()


def test_provider_defaults_cache_can_be_invalidated():
    from app.ai import provider_registry

    provider_registry._registry_providers()
    assert provider_registry._registry_providers_cache is not None

    provider_registry.invalidate_registry_providers_cache()
    assert provider_registry._registry_providers_cache is None


def test_model_pins_survive_a_node_rename(monkeypatch):
    """Пин модели к узлу хранится строкой и матчится по имени или id, но
    записывается всегда имя. После переименования узла ни одно сравнение не
    срабатывало, и select_instance молча уходил на первый попавшийся узел:
    модель, прибитая к конкретной машине, считалась на другой."""
    from app.api import providers_api

    store = {"qwen3_8_27b": "gpu-old", "embedder": "cpu-node"}
    monkeypatch.setattr("app.ai.model_registry._load_preferred_instances", lambda: dict(store))
    monkeypatch.setattr(
        "app.ai.model_registry.set_preferred_instance",
        lambda key, name: store.__setitem__(key, name),
    )

    providers_api._repin_models_after_rename("gpu-old", "gpu-new")

    assert store["qwen3_8_27b"] == "gpu-new"
    # Чужие пины не трогаем.
    assert store["embedder"] == "cpu-node"


# ── Пин хранится по id, а не по имени ────────────────────────────────────────


def test_pin_display_falls_back_to_the_raw_value(monkeypatch):
    """Пины, записанные до перехода на id, хранят имя — такое значение должно
    показываться как есть, а не превращаться в пустоту."""
    from app.api import providers_api

    monkeypatch.setattr(
        "app.ai.provider_registry._redis_get_instances",
        lambda: [{"id": "11111111-1111-1111-1111-111111111111", "name": "gpu-node"}],
    )
    assert providers_api._pin_display_name("11111111-1111-1111-1111-111111111111") == "gpu-node"
    assert providers_api._pin_display_name("старое-имя-узла") == "старое-имя-узла"
    assert providers_api._pin_display_name(None) is None


def test_resolver_takes_the_address_from_the_registry_only(monkeypatch):
    """`_provider_base_url`/`_provider_api_key` держали таблицу из 18 адресов и
    свой набор env-имён параллельно реестру. Две копии разошлись по пяти
    адресам сразу (api.moonshot.cn против .ai, dashscope без -intl,
    api.cohere.com против .ai) — и никто не замечал, потому что оба места
    «работали».

    Проверяем функционально: что вернул реестр, то и отдаёт резолвер. Если
    вторая таблица появится снова, подменённый реестр перестанет влиять."""
    from types import SimpleNamespace

    from app.ai import model_resolver

    monkeypatch.setattr(
        "app.ai.provider_registry.select_instance",
        lambda kind: SimpleNamespace(
            base_url="https://registry-decides.example/v1", api_key="key-from-registry"
        ),
    )
    for provider in ("anthropic", "openrouter", "groq", "cohere", "ollama"):
        assert model_resolver._provider_base_url(provider) == "https://registry-decides.example", (
            provider
        )
        assert model_resolver._provider_api_key(provider) == "key-from-registry"


def test_legacy_provider_aliases_resolve(monkeypatch):
    """`kimi` и `qwen` — исторические имена для moonshot и dashscope. Реестр их
    не знал, поэтому они уходили в захардкоженную таблицу с устаревшим
    адресом."""
    from app.ai import model_resolver

    seen: list[str] = []

    def fake_select(kind):
        seen.append(kind.value)
        return None

    monkeypatch.setattr("app.ai.provider_registry.select_instance", fake_select)
    model_resolver._resolved_node("kimi")
    model_resolver._resolved_node("qwen")
    assert seen == ["moonshot", "dashscope"]


def test_unknown_provider_yields_nothing_instead_of_someone_elses_address():
    """Незнакомый провайдер раньше получал адрес Ollama по умолчанию — так
    рождалась пара «облачное имя модели на локальном узле», падавшая 404."""
    from app.ai import model_resolver

    assert model_resolver._resolved_node("несуществующий") is None
    assert model_resolver._provider_base_url("несуществующий") == ""
    assert model_resolver._provider_api_key("несуществующий") == ""
