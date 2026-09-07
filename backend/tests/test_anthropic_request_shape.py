"""Claude тоже надо спрашивать — и просить у него столько, сколько нужно.

Три дефекта одного провайдера, найденные разбором маршрута оцифровки:

1. ``thinking: {"type": "enabled", "budget_tokens": N}`` — форма, удалённая
   Anthropic. На Opus 5 / 4.8 / 4.7, Sonnet 5 и семействе Fable/Mythos 5 она
   возвращает 400. Мы слали её ВСЕМ моделям Claude, поэтому любой вызов с
   включённым рассуждением падал на проводе, а роутер молча уходил на
   следующего кандидата: облачная модель выглядела «невыбранной», а не
   отвергнутой. Бюджет остался только у Haiku 4.5 и старше.

2. ``vision()`` читал вопрос только из ``prompt``/``input_text`` — ровно тот
   дефект, который уже чинили для OpenAI-совместимого провайдера (см.
   ``test_openai_vision_request.py``). Читатель чертежа задаёт вопрос через
   ``messages``, то есть Claude получал лист без вопроса, а системный промпт
   терялся целиком.

3. ``max_tokens`` был константой 4096, пока чтение чертежа просит 6000 и 8000.
   Ответ обрывался на середине JSON и засчитывался как неудачный проход.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.ai.providers.anthropic_provider import AnthropicProvider
from app.ai.schemas import AIRequest, AITask, ChatMessage, ProviderConfig, ProviderKind

IMAGE = "aGVsbG8="  # произвольные байты: важна форма запроса, не пиксель


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch):
    """Перехватить исходящий payload, не ходя в сеть."""
    sent: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"content": [{"type": "text", "text": "{}"}]}

    async def _post(self, url, **kw):
        sent.update(kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    return sent


def _provider() -> AnthropicProvider:
    return AnthropicProvider(
        ProviderConfig(
            kind=ProviderKind.ANTHROPIC,
            base_url="https://api.anthropic.com/v1",
            api_key="test",
            is_local=False,
        )
    )


def _image_request(**kwargs) -> AIRequest:
    return AIRequest(task=AITask.CAD_SPEC_READ, images=[IMAGE], **kwargs)


# ── 1. Рассуждение: форма зависит от поколения модели ────────────────────────


def test_current_models_get_adaptive_thinking(capture) -> None:
    request = AIRequest(
        task=AITask.ORCHESTRATOR_PLANNING,
        messages=[ChatMessage(role="user", content="Что делать?")],
        thinking=True,
        thinking_level="high",
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))
    assert capture["thinking"] == {"type": "adaptive"}
    assert capture["output_config"] == {"effort": "high"}
    assert "budget_tokens" not in str(capture["thinking"])


def test_legacy_models_keep_the_token_budget(capture) -> None:
    request = AIRequest(
        task=AITask.ORCHESTRATOR_PLANNING,
        messages=[ChatMessage(role="user", content="Что делать?")],
        thinking=True,
        thinking_level="medium",
    )
    asyncio.run(_provider().chat(request, "claude-haiku-4-5"))
    assert capture["thinking"] == {"type": "enabled", "budget_tokens": 4096}
    # Бюджет тратится ИЗ max_tokens — потолок обязан его перекрывать, иначе на
    # сам ответ места не остаётся.
    assert capture["max_tokens"] >= 4096 + 1024


def test_thinking_off_sends_no_reasoning_fields(capture) -> None:
    request = AIRequest(
        task=AITask.ORCHESTRATOR_PLANNING,
        messages=[ChatMessage(role="user", content="Что делать?")],
        thinking=False,
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))
    assert "thinking" not in capture
    assert "output_config" not in capture


# ── 2. Вопрос и системный промпт доходят до модели ───────────────────────────


def test_vision_question_comes_from_messages(capture) -> None:
    request = _image_request(
        messages=[ChatMessage(role="user", content="Какой тип детали на чертеже?")]
    )
    asyncio.run(_provider().vision(request, "claude-opus-5"))
    parts = [p["text"] for p in capture["messages"][0]["content"] if p.get("type") == "text"]
    assert parts == ["Какой тип детали на чертеже?"]


def test_vision_keeps_the_system_prompt(capture) -> None:
    request = _image_request(
        messages=[
            ChatMessage(role="system", content="Ты — инженер-конструктор."),
            ChatMessage(role="user", content="Прочитай штамп."),
        ]
    )
    asyncio.run(_provider().vision(request, "claude-opus-5"))
    assert "Ты — инженер-конструктор." in str(capture["system"])
    parts = [p["text"] for p in capture["messages"][0]["content"] if p.get("type") == "text"]
    assert parts == ["Прочитай штамп."]


def test_vision_plain_prompt_still_works(capture) -> None:
    """Старая форма вызова используется другими местами и ломаться не должна."""
    request = _image_request(prompt="Опиши деталь.")
    asyncio.run(_provider().vision(request, "claude-opus-5"))
    parts = [p["text"] for p in capture["messages"][0]["content"] if p.get("type") == "text"]
    assert parts == ["Опиши деталь."]


# ── 3. Лимит вывода — тот, который запросили ─────────────────────────────────


def test_num_predict_becomes_max_tokens(capture) -> None:
    request = _image_request(
        messages=[ChatMessage(role="user", content="Прочитай лист целиком.")],
        metadata={"num_predict": 8000},
    )
    asyncio.run(_provider().vision(request, "claude-opus-5"))
    assert capture["max_tokens"] == 8000


def test_inference_params_max_tokens_wins(capture) -> None:
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        metadata={"num_predict": 8000, "inference_params": {"max_tokens": 12000}},
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))
    assert capture["max_tokens"] == 12000


def test_missing_limit_falls_back_to_the_default(capture) -> None:
    request = AIRequest(task=AITask.CAD_SPEC_READ, messages=[ChatMessage(role="user", content="?")])
    asyncio.run(_provider().chat(request, "claude-opus-5"))
    assert capture["max_tokens"] == 4096


def test_absurd_limit_is_clamped_instead_of_rejected(capture) -> None:
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        metadata={"num_predict": 10_000_000},
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))
    assert capture["max_tokens"] == 128000


# ── Сэмплирующие параметры на актуальных моделях удалены ─────────────────────


def test_sampling_params_are_not_sent(capture) -> None:
    """Не «забыли прокинуть», а осознанно не шлём.

    ``temperature``/``top_p``/``top_k`` удалены на Opus 5 / 4.8 / 4.7, Sonnet 5
    и Fable 5/5.1 — запрос с ними возвращает 400. Роль профиля
    ``anti_hallucination`` здесь играет ``output_config.effort``, а не
    температура. Тест стоит, чтобы правку «добавим inference_params ради
    паритета с другими провайдерами» нельзя было внести молча.
    """
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        metadata={"inference_params": {"temperature": 0.0, "top_p": 1.0, "top_k": 1}},
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))
    assert "temperature" not in capture
    assert "top_p" not in capture
    assert "top_k" not in capture


# ── 4. Схема ответа: её не передавали вообще ─────────────────────────────────


def _with_contract(request: AIRequest, mechanism) -> AIRequest:
    from app.ai.output_format import FormatContract, attach

    return attach(
        request,
        FormatContract(
            schema={"type": "object", "properties": {"kind": {"type": "string"}}},
            schema_name="answer",
            mechanism=mechanism,
        ),
    )


def test_native_schema_goes_into_output_config(capture) -> None:
    """`output_config.format`, а не устаревший top-level `output_format`."""
    from app.ai.output_format import FormatMechanism

    request = _with_contract(
        AIRequest(task=AITask.CAD_SPEC_READ, messages=[ChatMessage(role="user", content="?")]),
        FormatMechanism.NATIVE_SCHEMA,
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))

    assert capture["output_config"]["format"]["properties"]["kind"]["type"] == "string"
    assert "output_format" not in capture
    # Принуждение схемой — форму словами повторять незачем.
    assert "строго по этой схеме" not in str(capture["messages"])


def test_forced_tool_sends_the_schema_as_a_tool_and_pins_the_choice(capture) -> None:
    from app.ai.output_format import FormatMechanism

    request = _with_contract(
        AIRequest(task=AITask.CAD_SPEC_READ, messages=[ChatMessage(role="user", content="?")]),
        FormatMechanism.FORCED_TOOL,
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))

    assert capture["tool_choice"] == {"type": "tool", "name": "answer"}
    assert capture["tools"][0]["name"] == "answer"
    assert capture["tools"][0]["input_schema"]["properties"]["kind"]["type"] == "string"


def test_the_forced_tool_answer_is_returned_as_text_and_data(monkeypatch) -> None:
    """Иначе ответ, полученный самым надёжным способом, никто бы не прочитал."""
    from app.ai.output_format import FormatMechanism

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "content": [
                    {"type": "tool_use", "name": "answer", "input": {"kind": "rotation_body"}}
                ],
                "usage": {},
            }

    async def _post(self, url, **kw):
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    request = _with_contract(
        AIRequest(task=AITask.CAD_SPEC_READ, messages=[ChatMessage(role="user", content="?")]),
        FormatMechanism.FORCED_TOOL,
    )
    response = asyncio.run(_provider().chat(request, "claude-opus-5"))

    assert response.data == {"kind": "rotation_body"}
    assert "rotation_body" in (response.text or "")
    # Это ответ, а не просьба модели вызвать инструмент.
    assert response.proposed_tool_calls == []


def test_the_weakest_rung_spells_the_schema_out_in_words(capture) -> None:
    from app.ai.output_format import FormatMechanism

    request = _with_contract(
        AIRequest(task=AITask.CAD_SPEC_READ, messages=[ChatMessage(role="user", content="?")]),
        FormatMechanism.PROMPT_ONLY,
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))

    assert "output_config" not in capture
    assert "tool_choice" not in capture
    assert "строго по этой схеме" in str(capture["messages"])


def test_vision_carries_the_schema_too(capture) -> None:
    from app.ai.output_format import FormatMechanism

    request = _with_contract(_image_request(prompt="?"), FormatMechanism.NATIVE_SCHEMA)
    asyncio.run(_provider().vision(request, "claude-opus-5"))

    assert "output_config" in capture


def test_a_request_without_a_schema_is_unchanged(capture) -> None:
    request = AIRequest(task=AITask.CAD_SPEC_READ, messages=[ChatMessage(role="user", content="?")])
    asyncio.run(_provider().chat(request, "claude-opus-5"))

    assert "output_config" not in capture
    assert "tools" not in capture


# ── 5. Кэш префикса — там, где запрос повторяется ────────────────────────────


def test_a_repeated_prefix_is_marked_for_caching(capture) -> None:
    """Чтение листа делает пять проходов с одним префиксом.

    Та же картинка, тот же вопрос, та же схема — повторное чтение префикса
    стоит около десятой доли обычного.
    """
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        metadata={"cache_prefix": True},
    )
    asyncio.run(_provider().chat(request, "claude-opus-5"))

    assert capture["cache_control"] == {"type": "ephemeral"}


def test_a_one_off_call_pays_nothing_for_the_cache(capture) -> None:
    """Запись в кэш дороже обычного чтения — на одиночном вызове не за что."""
    request = AIRequest(task=AITask.CAD_SPEC_READ, messages=[ChatMessage(role="user", content="?")])
    asyncio.run(_provider().chat(request, "claude-opus-5"))

    assert "cache_control" not in capture


def test_vision_marks_the_prefix_too(capture) -> None:
    """Основной объём префикса — картинка, и она идёт именно этим путём."""
    request = _image_request(prompt="?", metadata={"cache_prefix": True})
    asyncio.run(_provider().vision(request, "claude-opus-5"))

    assert capture["cache_control"] == {"type": "ephemeral"}
