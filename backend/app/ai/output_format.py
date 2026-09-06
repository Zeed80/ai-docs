"""Каким механизмом добиваться нужного формата ответа от КОНКРЕТНОЙ модели.

Решение о формате было размазано по провайдерам, и каждый решал по-своему:
Ollama клала схему в ``format``, OpenAI-совместимый выбирал между строгим
``json_schema`` и «просто JSON» плюс схема словами, Anthropic не делал ничего.
Из-за этого одна и та же задача получала разную степень принуждения в
зависимости от того, куда её направили, а разобраться, почему модель отвечает
не тем, было негде.

Здесь — только РЕШЕНИЕ: чистая функция от возможностей модели и запроса к
лестнице механизмов. Исполнение (как это выглядит на проводе) остаётся за
провайдером, восстановление (repair → переспрос → деградация) — за роутером:
только он владеет цепочкой кандидатов и её бюджетом.

Лестница, а не один выбор. Заявленные возможности модели недостоверны в обе
стороны: на этом стенде ``ollama_cloud deepseek-v3.1`` объявлен без зрения и
при этом прочитал чертёж, а ``openrouter minimax-m3:free`` объявлен кандидатом
и строгую схему не держит. Поэтому неудача на верхней ступени — не повод
терять кандидата целиком: спускаемся на ступень ниже и пробуем снова.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    from app.ai.schemas import AIRequest, ModelCapability

_CONTRACT_KEY = "format_contract"


class FormatMechanism(StrEnum):
    """Способы потребовать формат, от самого принудительного к самому слабому."""

    # Схема уходит параметром и соблюдается движком: Ollama `format=<схема>`,
    # OpenAI `response_format.json_schema` со `strict`, Anthropic
    # `output_config.format`.
    NATIVE_SCHEMA = "native_schema"
    # Anthropic: схема как input_schema инструмента + принудительный tool_choice.
    # Средняя ступень, не вершина: на новейших моделях принудительный выбор
    # инструмента отвергается с 400, поэтому он обязан быть деградируемым.
    FORCED_TOOL = "forced_tool"
    # «Ответь валидным JSON» без проверки формы. Форму приходится сообщать
    # словами — иначе приходит валидный, но чужой JSON (голый массив вместо
    # объекта с ожидаемым ключом).
    JSON_MODE = "json_mode"
    # Ничего, кроме просьбы в тексте. Последняя ступень.
    PROMPT_ONLY = "prompt_only"


# Виды провайдеров, где схема уходит параметром движка.
_LOCAL_KINDS = frozenset({"ollama", "llamacpp", "vllm", "lmstudio", "openai_compatible"})
_OPENAI_KINDS = frozenset(
    {
        "openai_compatible",
        "openrouter",
        "vllm",
        "lmstudio",
        "openai",
        "groq",
        "xai",
        "dashscope",
        "qwen",
        "cerebras",
        "mistral",
        "moonshot",
        "minimax",
        "together",
        "fireworks",
        "deepinfra",
        "nebius",
        "novita",
        "hyperbolic",
        "deepseek",
        "ollama_cloud",
        "perplexity",
        "cohere",
    }
)


@dataclass(frozen=True)
class FormatContract:
    """Что именно требуем от модели в этой попытке."""

    schema: dict[str, Any] | None
    schema_name: str
    mechanism: FormatMechanism
    # Ступени ниже текущей, в порядке спуска.
    remaining: tuple[FormatMechanism, ...] = ()
    # Номер переспроса (не деградации) — переспрос не меняет механизм.
    attempt: int = 0

    @property
    def ladder(self) -> tuple[FormatMechanism, ...]:
        return (self.mechanism, *self.remaining)


def requested_schema(request: AIRequest) -> dict[str, Any] | None:
    """JSON-схема, которую просит вызывающий: сырая из metadata или из модели.

    Два канала существуют не по недосмотру: ``response_schema`` — типизованный
    путь для python-вызывающих, ``metadata["json_schema"]`` — готовый словарь,
    которым пользуется весь ``cad_recognize`` (там схема собирается под каждый
    вопрос отдельно и класса под неё нет).
    """
    schema = (request.metadata or {}).get("json_schema")
    if schema is None and request.response_schema is not None:
        try:
            schema = request.response_schema.model_json_schema()
        except Exception:  # noqa: BLE001 — схема необязательна, JSON важнее
            schema = None
    return schema if isinstance(schema, dict) else None


def _model_holds_strict_schema(model: ModelCapability | None) -> bool:
    """Можно ли верить, что модель выдержит строгую схему.

    Неизвестность стоит одной ступени, а не проваленного кандидата: каталог
    заполняется автоматически, и 400 на строгой схеме сегодня обходится в
    целую модель из цепочки. Живая проба (``capability_source == "verified"``)
    поднимает модель обратно на верхнюю ступень.
    """
    if model is None:
        return False
    if getattr(model, "capabilities_unknown", False):
        return False
    return bool(getattr(model, "supports_structured_output", False))


def format_ladder(
    provider_kind: str, model: ModelCapability | None, request: AIRequest
) -> tuple[FormatMechanism, ...]:
    """Ступени принуждения к формату для этой пары «провайдер + модель»."""
    kind = (provider_kind or "").lower()

    if kind == "anthropic":
        ladder = [FormatMechanism.NATIVE_SCHEMA]
        # Принудительный выбор инструмента несовместим с двумя вещами сразу:
        # с собственными инструментами вызывающего (два разных принуждения) и
        # с расширенным рассуждением. В обоих случаях ступень пропускается.
        if not request.tools and not request.thinking:
            ladder.append(FormatMechanism.FORCED_TOOL)
        ladder.append(FormatMechanism.PROMPT_ONLY)
        return tuple(ladder)

    if kind in _LOCAL_KINDS and kind != "openai_compatible":
        # Ollama и совместимые движки принимают схему как есть и не ругаются
        # на неё; строгий режим здесь безопасен независимо от каталога.
        return (
            FormatMechanism.NATIVE_SCHEMA,
            FormatMechanism.JSON_MODE,
            FormatMechanism.PROMPT_ONLY,
        )

    if kind in _OPENAI_KINDS:
        if _model_holds_strict_schema(model):
            return (
                FormatMechanism.NATIVE_SCHEMA,
                FormatMechanism.JSON_MODE,
                FormatMechanism.PROMPT_ONLY,
            )
        return (FormatMechanism.JSON_MODE, FormatMechanism.PROMPT_ONLY)

    # Незнакомый вид провайдера: требовать формат параметром нельзя — просим
    # словами. Ошибиться здесь дешевле в сторону меньшего принуждения.
    return (FormatMechanism.PROMPT_ONLY,)


def initial_contract(
    request: AIRequest, model: ModelCapability | None, provider_kind: str
) -> FormatContract | None:
    """Верхняя ступень лестницы, или ``None``, если формат не запрашивали.

    ``None`` означает «поведение ровно прежнее»: без схемы ни requirement, ни
    переспрос не нужны, и путь embedding/rerank/обычного чата не меняется.
    """
    schema = requested_schema(request)
    if schema is None and request.response_schema is None:
        return None
    ladder = format_ladder(provider_kind, model, request)
    return FormatContract(
        schema=schema,
        schema_name="answer",
        mechanism=ladder[0],
        remaining=tuple(ladder[1:]),
    )


def degrade(contract: FormatContract | None) -> FormatContract | None:
    """Спуститься на ступень ниже; ``None`` — спускаться некуда."""
    if contract is None or not contract.remaining:
        return None
    return replace(
        contract,
        mechanism=contract.remaining[0],
        remaining=contract.remaining[1:],
        attempt=0,
    )


def with_attempt(contract: FormatContract, attempt: int) -> FormatContract:
    return replace(contract, attempt=attempt)


def schema_hint_text(contract: FormatContract | None) -> str:
    """Схема словами — единственный способ сообщить форму без принуждения.

    Нужна только на ступенях, где движок форму не проверяет. Без неё модель
    отвечала валидным, но чужим JSON: читатель чертежа ждёт ``{"frames": [...]}``,
    а приходил голый массив, и слой PMI терялся целиком.
    """
    if contract is None or contract.schema is None:
        return ""
    if contract.mechanism in (FormatMechanism.NATIVE_SCHEMA, FormatMechanism.FORCED_TOOL):
        return ""
    return (
        "\n\nОтветь ОДНИМ объектом JSON строго по этой схеме, без пояснений и "
        "без markdown-ограждений:\n" + json.dumps(contract.schema, ensure_ascii=False)
    )


def attach(request: AIRequest, contract: FormatContract | None) -> AIRequest:
    """Положить контракт в запрос, не трогая исходный объект вызывающего."""
    metadata = {**(request.metadata or {})}
    if contract is None:
        metadata.pop(_CONTRACT_KEY, None)
    else:
        metadata[_CONTRACT_KEY] = contract
    return request.model_copy(update={"metadata": metadata})


def contract_of(request: AIRequest) -> FormatContract | None:
    """Контракт, выбранный роутером. Провайдеры читают только это."""
    value = (request.metadata or {}).get(_CONTRACT_KEY)
    return value if isinstance(value, FormatContract) else None


# ── Подготовка схемы под требования конкретных API ───────────────────────────


def strictify_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Схема в виде, который принимает строгий режим OpenAI.

    ``strict: true`` требует, чтобы у каждого объекта стоял
    ``additionalProperties: false`` и чтобы ВСЕ его свойства были в
    ``required``. Схема, собранная Pydantic'ом, этому не отвечает, и запрос
    возвращается с 400 — то есть строгий режим без этой подготовки не работает
    вообще.
    """
    import copy

    prepared = copy.deepcopy(schema)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["additionalProperties"] = False
                    node["required"] = list(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(prepared)
    return prepared


def inline_schema_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Развернуть ``$ref``/``$defs`` — вложенные модели Pydantic ломают часть API.

    Anthropic ``input_schema`` и некоторые шлюзы ссылок не понимают и отвечают
    400 либо молча игнорируют схему. Циклические ссылки не разворачиваются:
    такая ветка остаётся пустым объектом, что честнее бесконечного раскрытия.
    """
    import copy

    defs = schema.get("$defs") or schema.get("definitions") or {}
    if not isinstance(defs, dict) or not defs:
        return schema

    def resolve(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                if name in seen or name not in defs:
                    return {"type": "object"}
                return resolve(copy.deepcopy(defs[name]), seen | {name})
            return {
                key: resolve(value, seen)
                for key, value in node.items()
                if key not in ("$defs", "definitions")
            }
        if isinstance(node, list):
            return [resolve(value, seen) for value in node]
        return node

    return resolve(copy.deepcopy(schema), frozenset())


# ── Различение двух классов отказа ───────────────────────────────────────────

# Слова, по которым шлюз ругается ИМЕННО на требование формата, а не на что-то
# ещё. Смешивать эти два случая нельзя: отказ провода лечится спуском на ступень
# ниже у ТОЙ ЖЕ модели, а невалидный ответ при HTTP 200 — переспросом.
_WIRE_REJECTION_MARKERS = (
    "response_format",
    "json_schema",
    "tool_choice",
    "output_config",
    "output_format",
    "strict",
    "structured output",
    "additionalproperties",
)
_STATUS_IN_TEXT = re.compile(r"\b(400|422)\b")


def classify_wire_rejection(exc: BaseException) -> bool:
    """Отверг ли шлюз само требование формата (а не содержимое запроса).

    Эвристика по тексту тела ответа, и это осознанно: ложное срабатывание лишь
    понизит ступень (безопасно), ложное отрицание вернёт прежнее поведение —
    переход к следующему кандидату.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    text = ""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            text = response.text or ""
        except Exception:  # noqa: BLE001 — тело может быть уже закрыто
            text = ""
    message = f"{exc} {text}".lower()
    if status not in (400, 422) and not _STATUS_IN_TEXT.search(str(exc)):
        return False
    return any(marker in message for marker in _WIRE_REJECTION_MARKERS)
