"""Версия в адресе провайдера не должна задваиваться.

Найдено живым вызовом: назначенная модель OpenRouter отвечала — но отвечала
локальная модель из фолбэка, а облачный запрос падал на
``https://openrouter.ai/api/v1/v1/chat/completions`` (404). Провайдер безусловно
дописывал ``/v1``, тогда как почти каждый облачный шлюз уже несёт версию в
base_url. Со стороны человека это выглядело как рабочая облачная модель:
ответ есть, назначение на месте, ошибка — строкой в журнале.
"""

from __future__ import annotations

import pytest

from app.ai.providers.openai_compatible import openai_endpoint

# base_url взяты из backend/app/ai/config/model_registry.yaml как есть.
CLOUD = [
    ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1/chat/completions"),
    ("https://api.openai.com/v1", "https://api.openai.com/v1/chat/completions"),
    ("https://ollama.com/v1", "https://ollama.com/v1/chat/completions"),
    ("https://api.groq.com/openai/v1", "https://api.groq.com/openai/v1/chat/completions"),
    (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions",
    ),
    (
        "https://api.fireworks.ai/inference/v1",
        "https://api.fireworks.ai/inference/v1/chat/completions",
    ),
    (
        "https://api.cohere.ai/compatibility/v1",
        "https://api.cohere.ai/compatibility/v1/chat/completions",
    ),
    (
        # Gemini в OpenAI-совместимом режиме: версия стоит не последней.
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    ),
]

LOCAL = [
    ("http://localhost:11436", "http://localhost:11436/v1/chat/completions"),
    ("http://localhost:8000", "http://localhost:8000/v1/chat/completions"),
    ("http://gpu-node:8080/", "http://gpu-node:8080/v1/chat/completions"),
]


@pytest.mark.parametrize(("base", "expected"), CLOUD)
def test_versioned_base_url_is_not_doubled(base: str, expected: str) -> None:
    assert openai_endpoint(base, "chat/completions") == expected


@pytest.mark.parametrize(("base", "expected"), LOCAL)
def test_bare_base_url_still_gets_the_version(base: str, expected: str) -> None:
    assert openai_endpoint(base, "chat/completions") == expected


def test_every_cloud_provider_in_the_registry_builds_a_sane_url() -> None:
    """Проверка не по списку выше, а по живому реестру провайдеров."""
    from app.ai.model_registry import ModelRegistry

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    for kind, provider in registry.providers.items():
        base = getattr(provider, "base_url", None)
        if not base:
            continue
        url = openai_endpoint(base, "chat/completions")
        assert "/v1/v1/" not in url, f"{kind}: {url}"
        assert url.endswith("/chat/completions"), f"{kind}: {url}"


@pytest.mark.parametrize("path", ["chat/completions", "embeddings", "rerank"])
def test_all_endpoints_share_the_rule(path: str) -> None:
    assert openai_endpoint("https://openrouter.ai/api/v1", path).count("/v1") == 1
    assert openai_endpoint("http://localhost:8000", path).endswith(f"/v1/{path}")
