"""Облачной модели вопрос к картинке надо ЗАДАТЬ.

Корень истории «облачная модель не может определить даже тип детали». Вопрос к
изображению брался только из ``prompt``/``input_text``, а весь читатель чертежа
задаёт его через ``messages`` — как и всё остальное в проекте. Для облачной
модели текстовая часть оказывалась пустой: она получала лист без единого
вопроса и отвечала вольным описанием — на английском, markdown-таблицами, в
разном порядке. Пайплайн отбрасывал такой ответ как «не JSON», все 29 запросов
чтения пропадали, и оцифровка падала на проверке типа детали.

Ollama-путь читает ``messages`` с самого начала, поэтому дефект был виден
только на облаке — и выглядел как глупость дорогой модели.

Живая проверка после исправления (та же модель, тот же лист z4-r4.jpg):
``{"kind": "rotation_body", "name": "Вал", "designation": "ПЭ-137.04.10.02.008.09",
"material": "Сталь 45 ГОСТ 1050-2014"}``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.ai.providers.openai_compatible import OpenAICompatibleProvider
from app.ai.schemas import AIRequest, AITask, ChatMessage, ProviderConfig, ProviderKind

IMAGE = "aGVsbG8="  # произвольные байты: важен не пиксель, а форма запроса


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch):
    """Перехватить исходящий payload, не ходя в сеть."""
    sent: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "{}"}}]}

    async def _post(self, url, **kw):
        sent.update(kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return sent


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        ProviderConfig(
            kind=ProviderKind.OPENROUTER,
            base_url="https://openrouter.ai/api/v1",
            is_local=False,
        )
    )


def _text_parts(sent: dict) -> list[str]:
    user = [m for m in sent["messages"] if m["role"] == "user"][0]
    return [p["text"] for p in user["content"] if p.get("type") == "text"]


def test_question_from_messages_reaches_the_model(capture) -> None:
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="Какой тип детали на чертеже?")],
        images=[IMAGE],
    )
    asyncio.run(_provider().vision(request, "vendor/model"))
    assert _text_parts(capture) == ["Какой тип детали на чертеже?"]


def test_system_prompt_is_not_dropped(capture) -> None:
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[
            ChatMessage(role="system", content="Ты — инженер-конструктор."),
            ChatMessage(role="user", content="Прочитай штамп."),
        ],
        images=[IMAGE],
    )
    asyncio.run(_provider().vision(request, "vendor/model"))
    assert capture["messages"][0] == {"role": "system", "content": "Ты — инженер-конструктор."}
    assert _text_parts(capture) == ["Прочитай штамп."]


def test_several_user_messages_are_joined(capture) -> None:
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[
            ChatMessage(role="user", content="Первый вопрос."),
            ChatMessage(role="assistant", content="Промежуточный ответ."),
            ChatMessage(role="user", content="Второй вопрос."),
        ],
        images=[IMAGE],
    )
    asyncio.run(_provider().vision(request, "vendor/model"))
    assert _text_parts(capture) == ["Первый вопрос.\n\nПромежуточный ответ.\n\nВторой вопрос."]


def test_plain_prompt_still_works(capture) -> None:
    """Старая форма вызова не должна сломаться — её используют другие места."""
    request = AIRequest(task=AITask.CAD_SPEC_READ, prompt="Опиши деталь.", images=[IMAGE])
    asyncio.run(_provider().vision(request, "vendor/model"))
    assert _text_parts(capture) == ["Опиши деталь."]


def test_image_is_wrapped_as_data_uri(capture) -> None:
    request = AIRequest(task=AITask.CAD_SPEC_READ, prompt="?", images=[IMAGE])
    asyncio.run(_provider().vision(request, "vendor/model"))
    user = [m for m in capture["messages"] if m["role"] == "user"][0]
    image_part = [p for p in user["content"] if p.get("type") == "image_url"][0]
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")


def test_vision_also_carries_the_json_requirement(capture) -> None:
    """Схема нужна и на картинке — читатель чертежа просит именно её."""
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        images=[IMAGE],
        metadata={"json_schema": {"type": "object"}},
    )
    asyncio.run(_provider().vision(request, "vendor/model"))
    assert capture["response_format"] == {"type": "json_object"}
