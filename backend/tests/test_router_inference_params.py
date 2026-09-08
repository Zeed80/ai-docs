"""Потолок вывода и параметры сэмплирования — один канал, а не два.

Раньше они жили только строковыми ключами в ``metadata``: без проверки типов и
без единой подсказки вызывающему, что такие ключи вообще бывают. Оттуда и
брались молчаливые потери — ``num_predict`` читался лишь в одном методе одного
провайдера, а ``min_p`` не читался нигде.

Теперь у ``AIRequest`` есть типизованные поля, а роутер сводит оба канала в
``_dispatch``: провайдеры по-прежнему читают ``metadata``, поэтому старые
вызовы менять не обязательно, но новый код пишет поле. Тест держит именно
сведение — разойдись оно, и половина вызовов снова начнёт терять параметры
бесшумно.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.ai.router import AIRouter
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AITask,
    Modality,
    ModelCapability,
    ModelStatus,
    ProviderKind,
)


class _Provider:
    """Провайдер, который только запоминает, что до него доехало."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] | None = None

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        self.seen = dict(request.metadata or {})
        return AIResponse(task=request.task, provider=ProviderKind.OLLAMA, model=model, text="ok")


def _model() -> ModelCapability:
    return ModelCapability(
        name="m",
        provider=ProviderKind.OLLAMA,
        provider_model="qwen3.8:27b",
        status=ModelStatus.PRODUCTION,
        modalities={Modality.TEXT},
        max_context_tokens=65536,
    )


def _dispatch(request: AIRequest) -> dict[str, Any]:
    provider = _Provider()
    asyncio.run(AIRouter()._dispatch(provider, request, _model()))
    assert provider.seen is not None
    return provider.seen


def test_the_typed_field_reaches_the_provider():
    seen = _dispatch(AIRequest(task=AITask.CLASSIFICATION, prompt="?", max_output_tokens=8000))
    assert seen["num_predict"] == 8000


def test_inference_params_field_reaches_the_provider():
    seen = _dispatch(
        AIRequest(task=AITask.CLASSIFICATION, prompt="?", inference_params={"num_ctx": 65536})
    )
    assert seen["inference_params"]["num_ctx"] == 65536


def test_the_metadata_channel_still_works():
    """Существующие вызовы менять не обязательно — иначе это не сведение."""
    seen = _dispatch(
        AIRequest(
            task=AITask.CLASSIFICATION,
            prompt="?",
            metadata={"num_predict": 512, "inference_params": {"temperature": 0}},
        )
    )
    assert seen["num_predict"] == 512
    assert seen["inference_params"]["temperature"] == 0


def test_the_field_wins_over_the_key_because_it_is_more_explicit():
    seen = _dispatch(
        AIRequest(
            task=AITask.CLASSIFICATION,
            prompt="?",
            max_output_tokens=8000,
            metadata={"num_predict": 512},
        )
    )
    assert seen["num_predict"] == 8000


def test_the_model_context_window_travels_with_the_request():
    """Каталог знает окно, ограничивает его провайдер — до этого одной
    константой на всех: модель с окном 131072 получала 65536."""
    seen = _dispatch(AIRequest(task=AITask.CLASSIFICATION, prompt="?"))
    assert seen["model_max_context_tokens"] == 65536


def test_a_request_without_params_still_gets_the_task_profile():
    """Профиль задачи — дефолт, а не замена: без него температура терялась."""
    seen = _dispatch(AIRequest(task=AITask.CLASSIFICATION, prompt="?"))
    assert "inference_params" in seen
