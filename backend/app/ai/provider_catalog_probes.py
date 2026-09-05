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


def _openrouter_capability(
    key: str, kind: ProviderKind, item: dict, today: str
) -> ModelCapability:
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
        supports_structured_output="response_format" in params,
        thinking_supported="reasoning" in params or "include_reasoning" in params,
        cost_per_1k_input=cost_in * 1000 if cost_in else None,
        cost_per_1k_output=cost_out * 1000 if cost_out else None,
        local_only=False,
        capability_source="discovered",
        notes=f"Из каталога {kind.value}, {today}.",
    )


def _unknown_capability(
    key: str, kind: ProviderKind, item: dict, today: str
) -> ModelCapability:
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
_PROBES = {
    ProviderKind.OPENROUTER: _openrouter_capability,
}


def capability_from_listing(
    key: str, kind: ProviderKind, item: dict
) -> ModelCapability:
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
