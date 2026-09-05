"""Конфиденциальная задача не должна получать пару, которой нет нигде.

Раньше при облачном провайдере подменялся только provider, а имя модели
оставалось облачным: в локальный Ollama уходил запрос вида `claude-sonnet-5`
и возвращал 404. То есть задача не «падала на локальную модель», а ломалась —
и это писалось в лог уровнем warning, среди сотен других строк.
"""

import pytest

from app.ai import model_resolver
from app.config import settings


@pytest.fixture
def cloud_routing(monkeypatch):
    """Как будто на конфиденциальную задачу назначена облачная модель."""

    def fake_resolve_model(task):
        return "claude-sonnet-5", "anthropic"

    monkeypatch.setattr(
        "app.ai.task_routing.resolve_model", fake_resolve_model, raising=True
    )


@pytest.mark.parametrize(
    "getter, expected_model",
    [
        ("get_ocr_model", settings.ollama_model_ocr),
        ("get_vlm_model", settings.ollama_model_vlm),
        ("get_verify_model", settings.ollama_model_ocr),
    ],
)
def test_cloud_model_on_a_local_only_task_falls_back_as_a_whole_pair(
    cloud_routing, getter, expected_model
):
    cfg = getattr(model_resolver, getter)()

    assert cfg.provider == "ollama"
    # Главное: имя модели тоже локальное. Гибрид «облачное имя на локальном
    # узле» не резолвится ни во что и падает 404 при первом же вызове.
    assert cfg.model == expected_model
    assert cfg.model != "claude-sonnet-5"
    assert cfg.is_local


def test_local_assignment_is_left_alone(monkeypatch):
    monkeypatch.setattr(
        "app.ai.task_routing.resolve_model",
        lambda task: ("qwen3.5:9b", "ollama"),
        raising=True,
    )
    cfg = model_resolver.get_ocr_model()
    assert (cfg.model, cfg.provider) == ("qwen3.5:9b", "ollama")


def test_reasoning_may_use_cloud_when_not_confidential(monkeypatch):
    """Reasoning — не конфиденциальная задача по умолчанию: облако допустимо.

    Проверяем, что ужесточение конфиденциальных путей не задело этот.
    """
    monkeypatch.setattr(
        "app.ai.task_routing.resolve_model",
        lambda task: ("claude-sonnet-5", "anthropic"),
        raising=True,
    )
    cfg = model_resolver.get_reasoning_model(confidential=False)
    assert cfg.provider == "anthropic"
    assert cfg.is_cloud

    blocked = model_resolver.get_reasoning_model(confidential=True)
    assert blocked.provider == "ollama"
    assert blocked.model == settings.ollama_model_reasoning


# ── Узлы из БД действуют и на прямом пути вызова ──────────────────────────────


def test_direct_call_path_uses_the_node_registry(monkeypatch):
    """`_provider_base_url`/`_provider_api_key` держали свою таблицу из 18
    захардкоженных адресов и набор env-имён, не заглядывая в provider_instances.
    Узел, заведённый в GUI (второй Ollama на другой машине, свой base_url, ключ
    из зашифрованного хранилища), на этот путь просто не действовал."""
    from types import SimpleNamespace

    from app.ai import model_resolver

    node = SimpleNamespace(base_url="http://gpu-node:11434/v1", api_key="secret-from-db")
    monkeypatch.setattr(model_resolver, "_resolved_node", lambda provider: node)

    # /v1 срезается — оба потребителя в ollama_client дописывают его сами,
    # иначе получилось бы /v1/v1.
    assert model_resolver._provider_base_url("ollama") == "http://gpu-node:11434"
    assert model_resolver._provider_api_key("anthropic") == "secret-from-db"


def test_legacy_aliases_reach_the_registry_instead_of_a_second_table():
    """`kimi` и `qwen` — не значения ProviderKind (в enum они moonshot и
    dashscope). Раньше реестр их не резолвил, и они уходили в захардкоженную
    таблицу, где адрес успел устареть. Теперь это псевдонимы."""
    from app.ai import model_resolver

    node = model_resolver._resolved_node("kimi")
    assert node is not None
    assert node.kind.value == "moonshot"
    # Адрес приходит из реестра, а не из таблицы с устаревшим api.moonshot.cn.
    assert "moonshot.ai" in model_resolver._provider_base_url("kimi")
