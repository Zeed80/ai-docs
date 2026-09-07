"""Невалидный ответ больше не стоит модели целиком.

Раньше ответ, не прошедший схему, означал переход к СЛЕДУЮЩЕЙ модели цепочки.
На живом чтении чертежа это стоило трёх проходов из пяти: `minimax-m3:free`
строгий структурный вывод не держит, отвечала почти по форме — и каждый такой
ответ выбрасывался вместе со всем, что модель успела прочитать.

Порядок восстановления: локальный ремонт → один переспрос с текстом ошибки →
спуск на ступень ниже по лестнице принуждения → и только потом другой кандидат.
Отказ ПРОВОДА (400 на само требование формата) переспрос не тратит.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pydantic import BaseModel

from app.ai.output_format import FormatMechanism, contract_of
from app.ai.router import AIRouter
from app.ai.schemas import (
    AIRequest,
    AIResponse,
    AITask,
    ChatMessage,
    Modality,
    ModelCapability,
    ModelStatus,
    ProviderKind,
)


class Answer(BaseModel):
    kind: str


def _model(**kwargs) -> ModelCapability:
    defaults = dict(
        name="m",
        provider=ProviderKind.OPENROUTER,
        provider_model="vendor/model",
        status=ModelStatus.CANDIDATE,
        modalities={Modality.TEXT},
        local_only=False,
        supports_structured_output=True,
    )
    defaults.update(kwargs)
    return ModelCapability(**defaults)


def _request(**kwargs) -> AIRequest:
    defaults = dict(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="Какой тип детали?")],
        response_schema=Answer,
        confidential=False,
    )
    defaults.update(kwargs)
    return AIRequest(**defaults)


class _Recorder:
    """Провайдер-заглушка: отдаёт заготовленные ответы и помнит запросы."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[AIRequest] = []
        self.contracts: list[FormatMechanism | None] = []

    async def dispatch(self, _provider, request, model):
        self.requests.append(request)
        contract = contract_of(request)
        self.contracts.append(contract.mechanism if contract else None)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return AIResponse(
            task=request.task, provider=model.provider, model=model.name, text=outcome
        )


def _run(router: AIRouter, recorder: _Recorder, request: AIRequest, model: ModelCapability):
    router._dispatch = recorder.dispatch  # type: ignore[method-assign]
    return asyncio.run(
        router._run_candidate(object(), request, model, per_call_timeout=None, deadline=None)
    )


def _router() -> AIRouter:
    return AIRouter.__new__(AIRouter)


def _http_400(body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return httpx.HTTPStatusError(
        "error", request=request, response=httpx.Response(400, text=body, request=request)
    )


# ── Ремонт спасает без единого лишнего вызова ────────────────────────────────


def test_a_markdown_wrapped_answer_is_repaired_locally():
    """Обёртка в ```json — не повод тратить переспрос."""
    recorder = _Recorder(['```json\n{"kind": "rotation_body"}\n```'])
    response = _run(_router(), recorder, _request(), _model())

    assert response.data.kind == "rotation_body"
    assert len(recorder.requests) == 1


def test_json_buried_in_prose_is_recovered():
    recorder = _Recorder(['Вот ответ: {"kind": "вал"} — надеюсь, помог.'])
    response = _run(_router(), recorder, _request(), _model())

    assert response.data.kind == "вал"
    assert len(recorder.requests) == 1


# ── Переспрос ────────────────────────────────────────────────────────────────


def test_an_unusable_answer_is_asked_again_once_and_then_succeeds():
    recorder = _Recorder(["не знаю, что это", '{"kind": "вал"}'])
    response = _run(_router(), recorder, _request(), _model())

    assert response.data.kind == "вал"
    assert len(recorder.requests) == 2


def test_the_reask_adds_messages_and_never_ends_on_the_assistant():
    """Заканчивать историю ассистентом — это prefill, а он даёт 400 у Claude."""
    recorder = _Recorder(["мусор", '{"kind": "вал"}'])
    _run(_router(), recorder, _request(), _model())

    second = recorder.requests[1]
    assert len(second.messages) > len(recorder.requests[0].messages)
    assert second.messages[-1].role == "user"
    assert any(m.role == "assistant" and "мусор" in m.content for m in second.messages)
    # Исходный вопрос никуда не делся — промпт ДОПОЛНЕН, а не подменён.
    assert second.messages[0].content == "Какой тип детали?"


def test_the_reask_budget_is_configurable_per_request():
    recorder = _Recorder(["мусор", "снова мусор", '{"kind": "вал"}'])
    request = _request(metadata={"format_max_reasks": 2})
    response = _run(_router(), recorder, request, _model())

    assert response.data.kind == "вал"
    assert len(recorder.requests) == 3


def test_zero_reasks_still_walks_the_ladder_once_per_rung():
    """Переспросов нет — но спуск по ступеням это не отменяет.

    Это разные средства: переспрос просит ту же модель ответить аккуратнее,
    спуск меняет САМ СПОСОБ принуждения. Отключив первое, второе терять незачем.
    """
    recorder = _Recorder(["мусор", '{"kind": "вал"}'])
    request = _request(metadata={"format_max_reasks": 0})
    response = _run(_router(), recorder, request, _model())

    assert response.data.kind == "вал"
    assert recorder.contracts == [FormatMechanism.NATIVE_SCHEMA, FormatMechanism.JSON_MODE]


# ── Спуск по лестнице ────────────────────────────────────────────────────────


def test_a_gateway_refusing_the_format_degrades_without_spending_a_reask():
    """400 на `response_format` лечится сменой ступени, а не переспросом."""
    recorder = _Recorder(
        [_http_400('{"error":{"message":"response_format is not supported"}}'), '{"kind": "вал"}']
    )
    response = _run(_router(), recorder, _request(), _model())

    assert response.data.kind == "вал"
    assert recorder.contracts[0] is FormatMechanism.NATIVE_SCHEMA
    assert recorder.contracts[1] is FormatMechanism.JSON_MODE


def test_an_unrelated_error_is_not_retried_here():
    """Сетевую ошибку чинит следующий кандидат, а не эта петля."""
    recorder = _Recorder([RuntimeError("connection reset")])
    with pytest.raises(RuntimeError):
        _run(_router(), recorder, _request(), _model())
    assert len(recorder.requests) == 1


def test_after_the_reasks_run_out_the_ladder_is_walked_down():
    request = _request(metadata={"format_max_reasks": 0})
    recorder = _Recorder(["мусор", "снова мусор", '{"kind": "вал"}'])
    response = _run(_router(), recorder, request, _model())

    assert response.data.kind == "вал"
    assert recorder.contracts == [
        FormatMechanism.NATIVE_SCHEMA,
        FormatMechanism.JSON_MODE,
        FormatMechanism.PROMPT_ONLY,
    ]


# ── Когда спасти не удалось ──────────────────────────────────────────────────


def test_a_confidential_task_fails_the_candidate_after_everything():
    from app.ai.router import AIStructuredOutputValidationError

    request = _request(confidential=True, metadata={"format_max_reasks": 0})
    recorder = _Recorder(["мусор", "мусор", "мусор"])
    with pytest.raises(AIStructuredOutputValidationError):
        _run(_router(), recorder, request, _model())


def test_a_non_confidential_task_gets_the_answer_as_is():
    request = _request(metadata={"format_max_reasks": 0})
    recorder = _Recorder(["мусор", "мусор", "мусор"])
    response = _run(_router(), recorder, request, _model())
    assert response.text == "мусор"


# ── Запросы без схемы не меняются вовсе ──────────────────────────────────────


def test_a_request_without_a_schema_makes_exactly_one_call():
    """Путь embedding/rerank/обычного чата обязан остаться прежним."""
    recorder = _Recorder(["любой текст"])
    request = AIRequest(task=AITask.EMBEDDING, input_text="x")
    response = _run(_router(), recorder, request, _model())

    assert response.text == "любой текст"
    assert len(recorder.requests) == 1
    assert recorder.contracts == [None]


# ── Схема словарём: раньше её не проверяли вообще ────────────────────────────


def test_a_dict_schema_is_validated_too():
    """`metadata["json_schema"]` — так работает весь cad_recognize.

    Такие ответы роутер не проверял ВООБЩЕ: негодный ответ уходил вызывающему
    как успех, без единого шанса на переспрос.
    """
    schema = {"type": "object", "required": ["kind"], "properties": {"kind": {"type": "string"}}}
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        metadata={"json_schema": schema},
        confidential=False,
    )
    recorder = _Recorder(['{"other": 1}', '{"kind": "вал"}'])
    response = _run(_router(), recorder, request, _model())

    assert len(recorder.requests) == 2
    assert '"kind"' in (response.text or "")


# ── Недостижимый кандидат не должен подменять собой причину ──────────────────


def test_a_cloud_fallback_is_skipped_on_a_confidential_task(monkeypatch):
    """Мёртвая запись в хвосте обрывала ход и называла себя причиной.

    `drawing_analysis_vlm` — задача из CONFIDENTIAL_TASKS, а её дефолтная
    цепочка несла `claude_sonnet_anthropic`, которая здесь не может выполниться
    никогда. Дойдя до неё после отказа всех локальных кандидатов, роутер
    обрывал ход с «Confidential task ... cannot use non-local model
    claude_sonnet» — то есть называл причиной модель, которую оператор не
    выбирал, вместо настоящей: не сработал ни один локальный кандидат.
    """
    from app.ai.model_registry import ModelRegistry
    from app.ai.router import AIRouter
    from app.ai.task_routing import TaskRouting

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter.__new__(AIRouter)
    router.registry = registry

    routing = TaskRouting(
        task="drawing_analysis_vlm",
        models=["qwen3_5_9b_ollama", "claude_sonnet_anthropic"],
        local_only=True,
    )
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _t: routing)

    tried: list[str] = []

    async def _always_fails(provider, request, model, **kwargs):
        tried.append(model.name)
        raise RuntimeError("локальный узел не ответил")

    monkeypatch.setattr(router, "_run_candidate", _always_fails)
    monkeypatch.setattr(router, "_resolve_provider", lambda model: (object(), None))
    monkeypatch.setattr(router, "_enforce_policy", lambda *a, **k: None)

    request = AIRequest(
        task=AITask.DRAWING_ANALYSIS_VLM,
        messages=[ChatMessage(role="user", content="?")],
        confidential=True,
    )
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(router.run(request))

    # Причина названа настоящая, а не чужая модель.
    assert "локальный узел не ответил" in str(exc.value)
    assert tried == ["qwen3_5_9b_ollama"]
    assert "claude_sonnet_anthropic" not in tried


def test_a_model_the_caller_named_still_fails_loudly(monkeypatch):
    """Осознанный выбор облачной модели обязан падать громко, а не пропускаться.

    Иначе назначение станет декоративным: оператор выбрал модель, а вызов молча
    ушёл на другую.
    """
    from app.ai.model_registry import ModelRegistry
    from app.ai.router import AIRouter
    from app.ai.task_routing import TaskRouting

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter.__new__(AIRouter)
    router.registry = registry

    routing = TaskRouting(
        task="drawing_analysis_vlm", models=["claude_sonnet_anthropic"], local_only=True
    )
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _t: routing)

    seen: list[str] = []

    async def _record(provider, request, model, **kwargs):
        seen.append(model.name)
        raise RuntimeError("не дошло")

    monkeypatch.setattr(router, "_run_candidate", _record)
    monkeypatch.setattr(router, "_resolve_provider", lambda model: (object(), None))

    request = AIRequest(
        task=AITask.DRAWING_ANALYSIS_VLM,
        messages=[ChatMessage(role="user", content="?")],
        confidential=True,
        preferred_model="claude_sonnet_anthropic",
    )
    with pytest.raises(Exception) as exc:
        asyncio.run(router.run(request))

    # Названная модель не отсеяна молча: политика сказала своё слово.
    assert "claude_sonnet_anthropic" in str(exc.value) or seen == ["claude_sonnet_anthropic"]


def test_the_reason_comes_from_the_model_that_tried(monkeypatch):
    """Нескачанный запасной в хвосте не должен подменять собой причину.

    Живой прогон чтения чертежа: 10 из 13 упавших фрагментных вопросов
    сообщили «Model gemma4:e4b is not served by any enabled ollama node» —
    про модель, которая к делу не относится вовсе. Отвечать пыталась голова
    цепочки и падала по своей причине, но `last_error` перезаписывался каждым
    следующим кандидатом, и наружу уходил последний.
    """
    from app.ai.model_registry import ModelRegistry
    from app.ai.router import AIRouter
    from app.ai.task_routing import TaskRouting

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter.__new__(AIRouter)
    router.registry = registry

    routing = TaskRouting(
        task="cad_spec_read",
        models=["qwen3_5_9b_ollama", "gemma4_e4b_ollama"],
        local_only=True,
    )
    monkeypatch.setattr("app.ai.task_routing.get_routing_for", lambda _t: routing)
    monkeypatch.setattr(router, "_enforce_policy", lambda *a, **k: None)

    def _resolve(model):
        if model.name == "gemma4_e4b_ollama":
            raise RuntimeError("Model gemma4:e4b is not served by any enabled ollama node")
        return (object(), None)

    async def _head_fails(provider, request, model, **kwargs):
        raise RuntimeError("модель ответила пустой строкой")

    monkeypatch.setattr(router, "_resolve_provider", _resolve)
    monkeypatch.setattr(router, "_run_candidate", _head_fails)

    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        confidential=True,
    )
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(router.run(request))

    assert "пустой строкой" in str(exc.value)
    assert "gemma4" not in str(exc.value)
