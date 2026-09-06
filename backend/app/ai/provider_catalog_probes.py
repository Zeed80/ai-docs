"""Разбор ответов /v1/models разных провайдеров в honest ModelCapability.

`refresh_models` регистрировал каждую подтянутую модель одинаково:
``modalities={TEXT, TOOL_CALLING}``, ``supports_tool_calling=True``,
``supports_structured_output=True`` — вслепую, для любого провайдера. Дальше
эти выдуманные значения участвовали в валидации назначения как факт, то есть
система утверждала о модели то, чего не проверяла.

Здесь два режима. Если провайдер отдаёт метаданные — берём их: OpenRouter
возвращает ``context_length``, ``pricing`` и ``supported_parameters`` прямо в
списке моделей, и всё это раньше выбрасывалось. Если не отдаёт — честно
помечаем возможности неизвестными вместо того, чтобы придумать.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from app.ai.schemas import Modality, ModelCapability, ModelStatus, ProviderKind

logger = structlog.get_logger()


def _as_float(value: Any) -> float | None:
    """Цена приходит строкой ("0.000003") и означает стоимость одного токена."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _openrouter_capability(key: str, kind: ProviderKind, item: dict, today: str) -> ModelCapability:
    """OpenRouter описывает модель достаточно, чтобы не гадать."""
    arch = item.get("architecture") or {}
    params = set(item.get("supported_parameters") or [])
    inputs = set(arch.get("input_modalities") or [])

    modalities: set[Modality] = {Modality.TEXT}
    if "image" in inputs:
        modalities.add(Modality.VISION)
    if "audio" in inputs:
        modalities.add(Modality.AUDIO)
    tools = "tools" in params or "tool_choice" in params
    if tools:
        modalities.add(Modality.TOOL_CALLING)

    pricing = item.get("pricing") or {}
    # pricing указана за один токен, а в каталоге хранится за тысячу.
    cost_in = _as_float(pricing.get("prompt"))
    cost_out = _as_float(pricing.get("completion"))

    return ModelCapability(
        name=key,
        provider=kind,
        provider_model=item.get("id", ""),
        status=ModelStatus.CANDIDATE,
        modalities=modalities,
        max_context_tokens=item.get("context_length") or None,
        supports_tool_calling=tools,
        # У OpenRouter это ДВА разных параметра: `response_format` — просьба
        # «ответь валидным JSON», `structured_outputs` — настоящая схема с
        # проверкой. Мы записывали первый как второй, и читатель чертежа слал
        # строгую схему модели, которая её молча игнорирует: ответ приходил
        # вольным текстом, а каталог утверждал, что схема поддержана.
        supports_structured_output="structured_outputs" in params,
        thinking_supported="reasoning" in params or "include_reasoning" in params,
        cost_per_1k_input=cost_in * 1000 if cost_in else None,
        cost_per_1k_output=cost_out * 1000 if cost_out else None,
        local_only=False,
        capability_source="discovered",
        notes=f"Из каталога {kind.value}, {today}.",
    )


def _context_length(item: dict) -> int | None:
    """Размер окна под тем именем, под каким его публикует конкретный шлюз.

    Само по себе окно не говорит о возможностях модели, но теряется оно зря:
    без него роутер не знает, сколько текста можно отдать, и всюду стоят
    зашитые числа. Поле называется по-разному у каждого семейства, поэтому
    перебираем известные имена, а не одно.
    """
    for field in (
        "context_length",  # OpenRouter
        "context_window",  # Groq
        "max_context_length",  # Mistral
        "max_input_tokens",  # Anthropic (с марта 2026)
        "max_model_len",  # vLLM-совместимые шлюзы
    ):
        value = item.get(field)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _context_only_capability(
    key: str, kind: ProviderKind, item: dict, today: str
) -> ModelCapability:
    """Шлюз сообщает окно контекста и больше ничего.

    Промежуточный, честный случай между «разобрали всё» и «не знаем ничего»:
    возможности остаются неподтверждёнными (их выяснит живая проба), но окно
    контекста — измеримый факт, и выбрасывать его вместе с остальным незачем.
    """
    return ModelCapability(
        name=key,
        provider=kind,
        provider_model=item.get("id", ""),
        status=ModelStatus.CANDIDATE,
        modalities=set(),
        max_context_tokens=_context_length(item),
        local_only=False,
        capabilities_unknown=True,
        capability_source="discovered",
        notes=(
            f"Из каталога {kind.value}, {today}. Провайдер сообщает только размер "
            f"контекста; остальные возможности не подтверждены — сделайте пробный запрос."
        ),
    )


def _capability_flags(item: dict) -> dict[str, bool] | None:
    """``capabilities`` у Mistral и Anthropic — словарь флагов; у Ollama — список.

    Возвращает ``None``, когда поля нет вообще: «провайдер молчит» и «провайдер
    сказал, что не умеет» — разные вещи, и склеивать их нельзя.
    """
    caps = item.get("capabilities")
    if isinstance(caps, dict):
        return {str(k): bool(v) for k, v in caps.items()}
    if isinstance(caps, list):
        return {str(name): True for name in caps}
    return None


def _mistral_capability(key: str, kind: ProviderKind, item: dict, today: str) -> ModelCapability:
    """Mistral перечисляет возможности прямо в листинге."""
    flags = _capability_flags(item)
    if flags is None:
        return _context_only_capability(key, kind, item, today)

    modalities: set[Modality] = {Modality.TEXT}
    if flags.get("vision"):
        modalities.add(Modality.VISION)
    tools = bool(flags.get("function_calling"))
    if tools:
        modalities.add(Modality.TOOL_CALLING)

    return ModelCapability(
        name=key,
        provider=kind,
        provider_model=item.get("id", ""),
        status=ModelStatus.CANDIDATE,
        modalities=modalities,
        max_context_tokens=_context_length(item),
        supports_tool_calling=tools,
        supports_structured_output=bool(flags.get("structured_outputs")),
        local_only=False,
        capability_source="discovered",
        notes=f"Из каталога {kind.value}, {today}.",
    )


def _anthropic_capability(key: str, kind: ProviderKind, item: dict, today: str) -> ModelCapability:
    """Листинг Anthropic с марта 2026 несёт окно, потолок вывода и возможности.

    Читаем защитно: до этого изменения в ответе были только ``id`` и
    ``display_name``, и старые прокси перед API отдают ровно их. Нет флагов —
    падаем в «известно только окно», а не выдумываем набор по имени модели.
    """
    flags = _capability_flags(item)
    if flags is None:
        return _context_only_capability(key, kind, item, today)

    modalities: set[Modality] = {Modality.TEXT}
    if flags.get("vision"):
        modalities.add(Modality.VISION)
    # Инструменты у Anthropic есть у всех моделей, где заявлен хоть какой-то
    # набор возможностей; отдельного флага может не быть.
    tools = bool(flags.get("tool_use", flags.get("function_calling", True)))
    if tools:
        modalities.add(Modality.TOOL_CALLING)

    return ModelCapability(
        name=key,
        provider=kind,
        provider_model=item.get("id", ""),
        status=ModelStatus.CANDIDATE,
        modalities=modalities,
        max_context_tokens=_context_length(item),
        supports_tool_calling=tools,
        # `output_config.format` — настоящая схема с проверкой, она есть у всех
        # актуальных моделей Claude. Отдельным флагом провайдер её не публикует.
        supports_structured_output=True,
        thinking_supported=bool(flags.get("extended_thinking", True)),
        local_only=False,
        capability_source="discovered",
        notes=f"Из каталога {kind.value}, {today}.",
    )


def _unknown_capability(key: str, kind: ProviderKind, item: dict, today: str) -> ModelCapability:
    """Провайдер метаданных не дал — так и записываем.

    Раньше здесь проставлялось «умеет инструменты и структурный вывод», и
    валидация назначения потом ссылалась на это как на факт. Пустой набор
    модальностей вместе с флагом `capabilities_unknown` даёт другую, честную
    формулировку: не «модель не умеет», а «мы не проверяли — сделайте пробный
    запрос».
    """
    return ModelCapability(
        name=key,
        provider=kind,
        provider_model=item.get("id", ""),
        status=ModelStatus.CANDIDATE,
        modalities=set(),
        local_only=False,
        capabilities_unknown=True,
        capability_source="discovered",
        notes=(
            f"Из каталога {kind.value}, {today}. Возможности не подтверждены: "
            f"провайдер не сообщает их в списке моделей."
        ),
    )


# Провайдеры, чей ответ мы умеем разбирать. Остальные попадают в честное
# «неизвестно» — добавить разбор можно по одной функции на семейство.
#
# Три уровня, а не два: полный разбор там, где провайдер описывает модель;
# «только окно контекста» там, где он публикует размер и молчит об остальном;
# «неизвестно» там, где в листинге нет ничего, кроме идентификатора. Средний
# уровень добавлен потому, что окно контекста терялось у половины облаков,
# а выдумывать по нему возможности всё равно нельзя.
_PROBES = {
    ProviderKind.OPENROUTER: _openrouter_capability,
    ProviderKind.MISTRAL: _mistral_capability,
    ProviderKind.ANTHROPIC: _anthropic_capability,
    ProviderKind.GROQ: _context_only_capability,
    ProviderKind.CEREBRAS: _context_only_capability,
    ProviderKind.XAI: _context_only_capability,
    ProviderKind.TOGETHER: _context_only_capability,
    ProviderKind.FIREWORKS: _context_only_capability,
    ProviderKind.DEEPINFRA: _context_only_capability,
    ProviderKind.NEBIUS: _context_only_capability,
    # OPENAI и DASHSCOPE намеренно НЕ здесь: их листинг несёт только `id`,
    # `created` и `owned_by`. Догадка по имени модели — ровно то, от чего
    # избавлялись, когда писали этот модуль.
}


def capability_from_listing(key: str, kind: ProviderKind, item: dict) -> ModelCapability:
    """ModelCapability по одной записи из /v1/models."""
    today = time.strftime("%Y-%m-%d")
    probe = _PROBES.get(kind)
    if probe is None:
        return _unknown_capability(key, kind, item, today)
    try:
        return probe(key, kind, item, today)
    except Exception as exc:  # noqa: BLE001 — формат ответа мог измениться
        logger.warning(
            "provider_catalog_probe_failed",
            provider=kind.value,
            model=item.get("id"),
            error=str(exc),
        )
        return _unknown_capability(key, kind, item, today)
