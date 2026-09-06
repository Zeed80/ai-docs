"""Выбор механизма, которым добиваются формата ответа.

Решение о формате было размазано по провайдерам, и каждый решал по-своему:
Ollama клала схему в `format`, OpenAI-совместимый выбирал между строгим
`json_schema` и «просто JSON», Anthropic не делал ничего. Одна и та же задача
получала разную степень принуждения в зависимости от того, куда её направили.

Лестница, а не один выбор: заявленные возможности модели недостоверны в обе
стороны (на стенде `ollama_cloud deepseek-v3.1` объявлен без зрения и прочитал
чертёж; `openrouter minimax-m3:free` объявлен кандидатом и строгую схему не
держит), поэтому неудача на верхней ступени не должна стоить кандидата целиком.
"""

from __future__ import annotations

import httpx

from app.ai.output_format import (
    FormatMechanism,
    attach,
    classify_wire_rejection,
    contract_of,
    degrade,
    format_ladder,
    initial_contract,
    inline_schema_defs,
    requested_schema,
    schema_hint_text,
    strictify_schema,
)
from app.ai.schemas import (
    AIRequest,
    AITask,
    ChatMessage,
    Modality,
    ModelCapability,
    ModelStatus,
    ProviderKind,
    ToolSpec,
)

SCHEMA = {"type": "object", "properties": {"kind": {"type": "string"}}}


def _model(**kwargs) -> ModelCapability:
    defaults = dict(
        name="m",
        provider=ProviderKind.OPENROUTER,
        provider_model="vendor/model",
        status=ModelStatus.CANDIDATE,
        modalities={Modality.TEXT},
        local_only=False,
    )
    defaults.update(kwargs)
    return ModelCapability(**defaults)


def _request(**kwargs) -> AIRequest:
    defaults = dict(
        task=AITask.CAD_SPEC_READ,
        messages=[ChatMessage(role="user", content="?")],
        metadata={"json_schema": SCHEMA},
    )
    defaults.update(kwargs)
    return AIRequest(**defaults)


# ── Откуда берётся схема ─────────────────────────────────────────────────────


def test_schema_is_read_from_both_channels():
    """`response_schema` — типизованный путь, `metadata["json_schema"]` — весь cad_recognize."""
    assert requested_schema(_request()) == SCHEMA

    from pydantic import BaseModel

    class Answer(BaseModel):
        kind: str

    typed = AIRequest(task=AITask.CAD_SPEC_READ, response_schema=Answer, metadata={})
    assert requested_schema(typed)["properties"]["kind"]["type"] == "string"


def test_no_schema_means_no_contract():
    """Без схемы поведение обязано остаться ровно прежним."""
    plain = AIRequest(task=AITask.EMBEDDING, input_text="x")
    assert initial_contract(plain, _model(), "ollama") is None


# ── Лестницы по видам провайдеров ────────────────────────────────────────────


def test_local_engines_start_with_the_schema_itself():
    ladder = format_ladder("ollama", _model(local_only=True), _request())
    assert ladder == (
        FormatMechanism.NATIVE_SCHEMA,
        FormatMechanism.JSON_MODE,
        FormatMechanism.PROMPT_ONLY,
    )


def test_a_gateway_model_without_confirmed_support_starts_lower():
    """400 на строгой схеме стоит целого кандидата — неизвестность дешевле.

    Ровно случай `openrouter minimax-m3:free`: объявлен кандидатом, строгую
    схему не держит, и все три прохода полного чтения на нём отваливались.
    """
    ladder = format_ladder("openrouter", _model(supports_structured_output=False), _request())
    assert ladder[0] is FormatMechanism.JSON_MODE
    assert FormatMechanism.NATIVE_SCHEMA not in ladder


def test_a_confirmed_gateway_model_gets_the_strict_mode():
    ladder = format_ladder("openrouter", _model(supports_structured_output=True), _request())
    assert ladder[0] is FormatMechanism.NATIVE_SCHEMA


def test_unknown_capabilities_cost_one_rung_not_a_candidate():
    model = _model(supports_structured_output=True, capabilities_unknown=True)
    assert format_ladder("openrouter", model, _request())[0] is FormatMechanism.JSON_MODE


def test_anthropic_puts_forced_tool_in_the_middle_not_on_top():
    """Принудительный выбор инструмента обязан быть деградируемым.

    На новейших моделях `tool_choice: any|tool` возвращает 400, поэтому он не
    может быть единственным способом получить схему.
    """
    ladder = format_ladder("anthropic", _model(), _request())
    assert ladder == (
        FormatMechanism.NATIVE_SCHEMA,
        FormatMechanism.FORCED_TOOL,
        FormatMechanism.PROMPT_ONLY,
    )


def test_forced_tool_is_skipped_when_the_caller_brought_its_own_tools():
    """Два разных принуждения одновременно невозможны."""
    request = _request(tools=[ToolSpec(name="t", description="d", input_schema={})])
    assert FormatMechanism.FORCED_TOOL not in format_ladder("anthropic", _model(), request)


def test_forced_tool_is_skipped_with_extended_thinking():
    request = _request(thinking=True)
    assert FormatMechanism.FORCED_TOOL not in format_ladder("anthropic", _model(), request)


def test_an_unknown_provider_kind_only_asks_in_words():
    assert format_ladder("нечто-новое", _model(), _request()) == (FormatMechanism.PROMPT_ONLY,)


# ── Спуск по лестнице ────────────────────────────────────────────────────────


def test_degrading_walks_the_ladder_down_and_then_stops():
    contract = initial_contract(_request(), _model(local_only=True), "ollama")
    assert contract.mechanism is FormatMechanism.NATIVE_SCHEMA

    second = degrade(contract)
    assert second.mechanism is FormatMechanism.JSON_MODE

    third = degrade(second)
    assert third.mechanism is FormatMechanism.PROMPT_ONLY
    assert degrade(third) is None


def test_degrading_resets_the_reask_counter():
    """Смена механизма — не продолжение переспросов, а другая попытка."""
    from app.ai.output_format import with_attempt

    contract = with_attempt(initial_contract(_request(), _model(local_only=True), "ollama"), 1)
    assert degrade(contract).attempt == 0


# ── Схема словами — только там, где движок форму не проверяет ────────────────


def test_the_schema_is_spelled_out_only_when_nothing_enforces_it():
    contract = initial_contract(_request(), _model(local_only=True), "ollama")
    assert schema_hint_text(contract) == ""

    weak = degrade(degrade(contract))
    hint = schema_hint_text(weak)
    assert "строго по этой схеме" in hint
    assert '"kind"' in hint


# ── Контракт едет в запросе, не мутируя исходный ─────────────────────────────


def test_attaching_a_contract_does_not_mutate_the_callers_request():
    request = _request()
    contract = initial_contract(request, _model(local_only=True), "ollama")

    attached = attach(request, contract)

    assert contract_of(attached) is contract
    assert contract_of(request) is None
    assert request.metadata == {"json_schema": SCHEMA}


# ── Подготовка схемы под требования API ──────────────────────────────────────


def test_strict_mode_needs_every_property_required_and_closed_objects():
    """Без этого `strict: true` возвращает 400, то есть режим не работает вовсе."""
    prepared = strictify_schema(
        {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}}
    )
    assert prepared["additionalProperties"] is False
    assert sorted(prepared["required"]) == ["a", "b"]


def test_nested_models_are_inlined_because_refs_break_some_apis():
    schema = {
        "type": "object",
        "properties": {"step": {"$ref": "#/$defs/Step"}},
        "$defs": {"Step": {"type": "object", "properties": {"d": {"type": "number"}}}},
    }
    inlined = inline_schema_defs(schema)

    assert "$defs" not in inlined
    assert inlined["properties"]["step"]["properties"]["d"]["type"] == "number"


def test_a_recursive_schema_does_not_expand_forever():
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#/$defs/Node"}},
        "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
    }
    inlined = inline_schema_defs(schema)
    assert inlined["properties"]["child"]["properties"]["child"] == {"type": "object"}


# ── Два класса отказа не должны смешиваться ──────────────────────────────────


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, text=body, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


def test_a_gateway_refusing_the_format_requirement_is_recognised():
    """Лечится спуском на ступень ниже у ТОЙ ЖЕ модели, а не сменой модели."""
    exc = _http_error(400, '{"error":{"message":"response_format is not supported"}}')
    assert classify_wire_rejection(exc) is True


def test_forced_tool_rejection_is_recognised():
    exc = _http_error(400, 'tool_choice: type "tool" and "any" are not supported for this model.')
    assert classify_wire_rejection(exc) is True


def test_an_unrelated_error_is_not_mistaken_for_a_format_refusal():
    assert classify_wire_rejection(_http_error(400, "context length exceeded")) is False
    assert classify_wire_rejection(_http_error(429, "rate limit")) is False
    assert classify_wire_rejection(RuntimeError("boom")) is False


def test_a_server_error_is_never_a_format_refusal():
    assert classify_wire_rejection(_http_error(500, "response_format")) is False
