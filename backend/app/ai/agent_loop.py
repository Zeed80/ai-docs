"""Built-in agent loop — Ollama tool calling via /api/chat."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog
import yaml

from app.ai.agent_config import BuiltinAgentConfig, get_builtin_agent_config
from app.ai.capability_manifest import load_capability_manifest
from app.ai.degradation import log_degraded
from app.ai.gateway_config import gateway_config
from app.ai.streaming_scrubber import StreamingContextScrubber
from app.ai.thinking_params import REASONING_EFFORT_PROVIDERS as _REASONING_EFFORT_PROVIDERS
from app.ai.thinking_params import thinking_request_params as _thinking_request_params
from app.config import settings as _settings

logger = structlog.get_logger()

# Public cross-module contract (A3): everything else in this file is private
# to agent_loop.py — orchestrator.py, scenario_runner.py and flow_awareness.py
# import only these names, no other `_`-prefixed symbol. Before adding a new
# cross-module import, either it belongs here (rename, drop the leading
# underscore, add it below) or the caller shouldn't be reaching this deep into
# agent_loop's internals in the first place.
__all__ = [
    "AgentSession",
    "execute_skill",
    "extract_list_count",
    "load_registry",
    "sanitize_name",
    "internal_headers",
]


def internal_headers() -> dict:
    """Headers for agent → backend service calls (auth + internal marker).

    Carries the acting user (app.ai.actor_context) so endpoints can scope
    per-user data instead of seeing the full-admin service account. Absent on
    headless turns — endpoints must then fail closed.
    """
    from app.ai.actor_context import get_acting_user

    h: dict = {"X-Internal-Agent": "1"}
    if _settings.agent_service_key:
        h["X-API-Key"] = _settings.agent_service_key
    actor = get_acting_user()
    if actor:
        h["X-Acting-User"] = actor
    return h


def capability_args_digest(args: dict) -> str:
    """Отпечаток аргументов вызова — то, к чему привязано одобрение человека.

    Считается по тем же правилам на обеих сторонах: здесь — перед отправкой,
    в app.api.capability_router — по фактически пришедшему телу запроса
    (см. ``_enforce_capability_policy``).
    """
    import hashlib

    payload = {k: v for k, v in (args or {}).items() if k != "reason"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


_capability_args_digest = capability_args_digest


# Max chars for a single tool result stored in the LLM message history.
# Large lists (invoices, inventory, etc.) can easily hit 100k+ chars which
# triggers unnecessary context compression. Keep enough for the model to
# extract counts, statuses and a sample of items.
# Results above turn_vault.VAULT_THRESHOLD are stored in Redis and replaced
# with a compact envelope (preview + vault_ref) before reaching this trim.
_MAX_TOOL_RESULT_CHARS = 5000
# Number of list items to keep in the sample shown to the LLM.
_TOOL_RESULT_SAMPLE_ITEMS = 8
# Minimum items to always keep regardless of size.
_TOOL_RESULT_MIN_ITEMS = 3

# Heavy fields that can be stripped from items to reduce size.
_HEAVY_ITEM_FIELDS = {
    "description", "notes", "raw_text", "content", "body",
    "user_notes", "address", "comment", "history",
}


def _trim_tool_result(content: str) -> str:
    """Trim large tool result to fit within _MAX_TOOL_RESULT_CHARS.

    Strategy:
    1. Try progressively smaller samples (15 → 10 → 5 → min).
    2. If still over limit, strip heavy text fields from items.
    3. Never go below _TOOL_RESULT_MIN_ITEMS — model needs data to answer.
    4. Fallback: hard-truncate the string.
    """
    if len(content) <= _MAX_TOOL_RESULT_CHARS:
        return content
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            list_key = next((k for k in ("items", "results", "hits") if k in data), None)
            if list_key is not None:
                items = data.get(list_key) or []
                total = data.get("total", len(items))
                meta = {k: v for k, v in data.items() if k != list_key}

                def _build(sample: list, strip_heavy: bool = False) -> str:
                    if strip_heavy:
                        sample = [
                            {k: v for k, v in item.items() if k not in _HEAVY_ITEM_FIELDS}
                            if isinstance(item, dict) else item
                            for item in sample
                        ]
                    candidate = {**meta, list_key: sample}
                    candidate["_note"] = (
                        f"[total={total}. Показано {len(sample)} из {len(items)}. "
                        f"Один вызов достаточен.]"
                    )
                    return json.dumps(candidate, ensure_ascii=False)

                # Try progressively smaller samples (never below min)
                for n in (_TOOL_RESULT_SAMPLE_ITEMS, 10, 5, _TOOL_RESULT_MIN_ITEMS):
                    if n > len(items):
                        continue
                    result = _build(items[:n])
                    if len(result) <= _MAX_TOOL_RESULT_CHARS:
                        return result

                # Try stripping heavy fields from min-item sample
                result = _build(items[:_TOOL_RESULT_MIN_ITEMS], strip_heavy=True)
                if len(result) <= _MAX_TOOL_RESULT_CHARS:
                    return result

                # If even 3 stripped items are too big, fall through to truncation
    except (json.JSONDecodeError, TypeError, StopIteration):
        pass
    return content[:_MAX_TOOL_RESULT_CHARS] + f"\n...[truncated — original {len(content)} chars]"


_OPERATIONAL_POLICY = """
Принципы работы:
- Данные только из инструментов: никогда не выдумывай числа, суммы, статусы.
  Вызови нужный инструмент и дай результат.
- Действуй сразу: если сущность названа — выполняй, не переспрашивай.
  Уточняй только когда запрос объективно неоднозначен.
- Не комментируй процесс: не пиши «сейчас вызову инструмент» — просто вызови.
- Gates [GATE]: перед утверждением/отклонением счёта, отправкой письма,
  массовым удалением, оплатой, подтверждением прихода — покажи превью и
  дождись явного «да» от пользователя.
- Рабочий стол: используй workspace.* ТОЛЬКО если оркестратор явно указал
  canvas_id. Иначе — пиши краткий ответ в чат.
- Один вызов достаточен: не повторяй один и тот же инструмент с разными
  параметрами без явной причины. Получил результат → сформулируй ответ.
- Capability gap: если нужного инструмента нет — вызови capability.propose
  с описанием и планом реализации.
- Память автоматическая: для RAG сначала используй memory.query. Для связей
  между сущностями используй memory.neighborhood/path. Если из session-памяти
  нашёл полезный проверяемый факт — создай reviewable proposal через
  memory.promote, не записывай его сразу в project/global.
- Инициативность: если для задачи нужны внешние каталоги, прайсы или сайты
  поставщиков, сначала проверь memory.source_list. Если источника нет — создай
  предложение через memory.source_propose с URL/доменом и rationale. Если видишь
  полезную самостоятельную работу, но она не была прямо поручена, создай
  agent_control.task_propose вместо молчания или самовольного запуска.
- Решения approve/reject и запуск proposed-задач/источников/фактов (decide/run)
  принимает человек в GUI. У тебя нет этих действий — ты только предлагаешь.
- Пустой результат — это ответ, а не повод импровизировать: если названная
  сущность (поставщик, счёт, статус) не найдена или фильтр не дал строк, так
  и скажи прямым текстом («Поставщик «Х» не найден» / «Счетов в статусе
  «на проверке» сейчас нет») и остановись. Не подменяй нерелевантным запросом
  (например сводкой по всем статусам) и не публикуй на Рабочий стол пустые
  0-строчные таблицы как будто задача выполнена.
""".strip()


def _normalize_ru_yo(text: str) -> str:
    return text.replace("ё", "е").replace("Ё", "Е")


def _agent_canvas_id(kind: str) -> str:
    return f"agent:{kind}"


def _is_workspace_output_request(text: str) -> bool:
    t = _normalize_ru_yo((text or "").lower())
    return any(
        marker in t
        for marker in (
            "таблиц", "полный список", "все списком", "выведи список",
            "ссылк", "документ", "чертеж", "чертёж",
            "график", "диаграм", "excel", "скача", "файл",
            "столбец", "столбц", "колонк", "добавь поле", "убери поле",
            "отсортируй", "сортировк",
        )
    )


def _get_agent_model(
    config: BuiltinAgentConfig | None = None,
    *,
    model_override: str | None = None,
) -> str:
    """Current agent model: built-in config → ai_settings override → gateway default.

    Built-in agent config is used as primary source because provider/model are
    edited together in the Agent settings UI. ``model_agent`` from ``ai_config``
    remains a backward-compatible fallback and is kept in sync via API handlers.
    """
    if model_override and model_override.strip():
        return model_override.strip()
    if config and config.department_enabled and config.worker_model:
        return config.worker_model
    if config and config.model:
        return config.model
    try:
        # Раньше здесь читался ai_config.model_agent — второе хранилище той же
        # настройки. Модель агента задаётся слотом «Оркестратор», который
        # пишет и в agent_config, и в маршрутизацию задачи orchestrator_planning;
        # берём оттуда, чтобы значение не зависело от того, каким путём его
        # меняли в последний раз.
        from app.ai.schemas import AITask
        from app.ai.task_routing import resolve_model

        model, _provider = resolve_model(AITask.ORCHESTRATOR_PLANNING)
        if model and str(model).strip():
            return str(model).strip()
    except Exception as exc:
        log_degraded("agent_loop.model_override", exc)
    return gateway_config.reasoning_model


def _get_agent_provider(
    config: BuiltinAgentConfig,
    *,
    provider_override: str | None = None,
) -> str:
    if provider_override and provider_override.strip():
        return provider_override.strip()
    if config.department_enabled and config.worker_provider:
        return config.worker_provider
    return config.provider or "ollama"


def _is_builder_turn(messages: list[dict]) -> bool:
    latest_user = next(
        (
            str(m.get("content") or "")
            for m in reversed(messages)
            if m.get("role") == "user"
        ),
        "",
    ).lower()
    if not latest_user:
        return False
    builder_markers = (
        "skill",
        "скилл",
        "tool",
        "инструмент",
        "capability",
        "возможност",
        "plugin",
        "плагин",
        "script",
        "скрипт",
        "api",
        "endpoint",
        "код",
        "реализуй",
        "доработай",
        "создай",
    )
    return any(marker in latest_user for marker in builder_markers)


def _turn_model_overrides(
    config: BuiltinAgentConfig,
    messages: list[dict],
) -> tuple[str | None, str | None, bool | None, str | None]:
    if _is_builder_turn(messages):
        return (
            config.builder_model or config.worker_model,
            config.builder_provider or config.worker_provider,
            config.builder_disable_thinking,
            config.builder_thinking_level,
        )
    return (
        config.worker_model,
        config.worker_provider,
        config.worker_disable_thinking,
        config.worker_thinking_level,
    )


def _model_thinking_default(model_name: str | None) -> bool | None:
    """Catalog thinking_enabled for a model (by key or provider_model).

    Returns True/False when the model is found in the registry, else None.
    A model without ``thinking_supported`` resolves to False (no CoT).
    """
    if not model_name:
        return None
    try:
        from app.ai.router import ai_router

        for cap in ai_router.registry.models.values():
            if cap.name == model_name or cap.provider_model == model_name:
                return cap.thinking_enabled if cap.thinking_supported else False
    except Exception:
        return None
    return None


def _model_thinking_levels(model_name: str | None) -> list[str]:
    """Effective thinking_levels for a model (by key or provider_model), or [].

    Explicit catalog curation wins; otherwise auto-derived for providers
    where the level parameter is a guaranteed wire feature (Anthropic,
    reasoning_effort family, OpenRouter) — see effective_thinking_levels.
    """
    if not model_name:
        return []
    try:
        from app.ai.router import ai_router

        for cap in ai_router.registry.models.values():
            if cap.name == model_name or cap.provider_model == model_name:
                return _thinking_request_levels(cap)
    except Exception:
        return []
    return []


def _thinking_request_levels(cap) -> list[str]:
    from app.ai.thinking_params import effective_thinking_levels

    return effective_thinking_levels(cap.thinking_supported, cap.provider.value, cap.thinking_levels)


def _model_thinking_level_default(model_name: str | None) -> str | None:
    """Catalog default reasoning-effort level for a model, or None."""
    if not model_name:
        return None
    try:
        from app.ai.router import ai_router

        for cap in ai_router.registry.models.values():
            if cap.name == model_name or cap.provider_model == model_name:
                levels = _thinking_request_levels(cap)
                if not levels:
                    return None
                return cap.thinking_level_default or "medium"
    except Exception:
        return None
    return None


def _thinking_disabled(
    config: BuiltinAgentConfig,
    override: bool | None = None,
    model_name: str | None = None,
) -> bool:
    """Resolve whether thinking/CoT is OFF for this call.

    Priority: explicit per-role override → the model's catalog default
    (UI checkbox) → the global ``disable_thinking`` fallback.
    """
    if override is not None:
        return override
    model_default = _model_thinking_default(model_name)
    if model_default is not None:
        return not model_default  # thinking_enabled=True → not disabled
    return config.disable_thinking


def _thinking_level(
    config: BuiltinAgentConfig,
    *,
    disabled: bool,
    level_override: str | None = None,
    model_name: str | None = None,
) -> str | None:
    """Resolve the effective reasoning-effort level for this call.

    Priority: explicit per-role override → the model's catalog default level
    → the global fallback. Returns None whenever thinking is off for this
    call, or the model doesn't declare any levels at all — a level is
    meaningless in either case.
    """
    if disabled:
        return None
    levels = _model_thinking_levels(model_name)
    if not levels:
        return None
    level = level_override
    if level is None:
        level = _model_thinking_level_default(model_name)
    if level is None:
        level = config.thinking_level
    if level is None:
        level = "medium"
    if level not in levels:
        level = levels[0]
    return level


# ── Registry loading ──────────────────────────────────────────────────────────

def sanitize_name(name: str) -> str:
    """Replace dots with __ for OpenAI-compatible function names."""
    return name.replace(".", "__")


# Gate actions map for capabilities mode: capability_name → set of gate actions.
# Populated by _load_capabilities() and checked in _execute_single_tool().
_CAPABILITY_GATE_ACTIONS: dict[str, set[str]] = {}


def _registry_mtime() -> float:
    """Return mtime of the active skills file (capabilities or registry), or 0.0."""
    try:
        return gateway_config.active_skills_path.stat().st_mtime
    except Exception:
        return 0.0


# Global weak set of all active AgentSession instances for hot-reload signalling.
import weakref as _weakref
_ACTIVE_SESSIONS: "_weakref.WeakSet[AgentSession]" = _weakref.WeakSet()  # type: ignore[assignment]


async def deliver_external_approval(db_id: str, approved: bool) -> bool:
    """Донести решение, принятое ВНЕ вкладки чата, до ждущего хода.

    Запрос подтверждения жил только в WebSocket-сессии: человек, закрывший
    вкладку или ответивший со страницы согласований (или с телефона), обрывал
    ход — durable-запись Approval при этом создавалась, но продолжать было
    некому. Теперь решение по этой записи будит ту же сессию, если она ещё
    жива в этом процессе; если нет — поведение прежнее, ход давно закончился
    по таймауту.

    Возвращает True, если решение действительно кому-то доставлено.
    """
    for session in list(_ACTIVE_SESSIONS):
        if getattr(session, "_pending_db_id", None) != db_id:
            continue
        try:
            await session.on_approval(
                approved, approval_id=getattr(session, "_pending_approval_id", None)
            )
            logger.info(
                "approval_delivered_out_of_band", db_id=db_id, approved=approved,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("approval_delivery_failed", db_id=db_id, error=str(exc))
    return False


def reload_all_sessions() -> int:
    """Tell every live AgentSession to reload its skill map from the registry.

    Called by CapabilityBuilder after writing a new generated skill.
    Returns the number of sessions reloaded.
    """
    count = 0
    for session in list(_ACTIVE_SESSIONS):
        try:
            session.reload_skills()
            count += 1
        except Exception as exc:
            logger.warning("session_reload_failed", error=str(exc))
    logger.info("reload_all_sessions_done", count=count)
    return count


def _load_capabilities() -> tuple[list[dict], dict[str, dict]]:
    """Load capabilities.yml — broad capability tools for the agent.

    Each capability maps to POST /api/agent/cap/{name}. The agent supplies
    an `action` field; the backend dispatcher routes to the real endpoint.
    """
    global _CAPABILITY_GATE_ACTIONS
    cap_path = gateway_config.capabilities_path
    if not cap_path.exists():
        logger.warning("capabilities_not_found", path=str(cap_path))
        return [], {}

    manifest = load_capability_manifest(cap_path)

    # _DISPATCH is the single source of truth for valid actions; inject it as a
    # JSON-schema enum so the model can only emit a routable action (no more
    # "400 Unknown action" from guessed strings). Read live → drift-proof.
    try:
        from app.api.capability_router import capability_action_map
        action_enum = capability_action_map()
    except Exception as exc:
        log_degraded("agent_loop.action_enum", exc)
        action_enum = {}

    tools: list[dict] = []
    skill_map: dict[str, dict] = {}
    gate_actions: dict[str, set[str]] = {}

    for definition in manifest.capabilities:
        cap = definition.model_dump(mode="python")
        name = definition.name
        if not name:
            continue

        params_schema = cap.get("parameters") or {}
        properties: dict[str, Any] = {}
        required: list[str] = []

        if params_schema.get("properties"):
            for k, v in params_schema["properties"].items():
                properties[k] = {kk: vv for kk, vv in v.items() if kk not in ("title",)}
        if params_schema.get("required"):
            required = list(params_schema["required"])

        # Constrain `action` to the dispatcher's real enum for this capability.
        if "action" in properties and name in action_enum:
            properties["action"]["enum"] = action_enum[name]

        fn_name = sanitize_name(name)
        description = (cap.get("description") or name).strip()

        tools.append({
            "type": "function",
            "function": {
                "name": fn_name,
                # Keep the full curated description (actions + rules); 400 chars
                "description": description[:1200],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
        skill_entry = {
            "name": name,
            "method": cap.get("method", "POST"),
            "path": cap.get("path", f"/api/agent/cap/{name}"),
            "gate_actions": cap.get("gate_actions") or [],
        }
        skill_map[fn_name] = skill_entry
        if cap.get("gate_actions"):
            gate_actions[name] = set(cap["gate_actions"])

    # Promoted agent-generated skills (separate auto-managed file; the
    # hand-written capabilities.yml is never rewritten programmatically).
    # They execute in the isolated skill-runner via their registered path.
    gen_path = cap_path.with_name("capabilities.generated.yml")
    if gen_path.exists():
        try:
            gen_data = yaml.safe_load(gen_path.read_text()) or {}
            for entry in gen_data.get("generated") or []:
                gen_name = str(entry.get("name") or "")
                fn_name = sanitize_name(gen_name)
                if not gen_name or fn_name in skill_map:
                    continue
                tools.append({
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "description": str(entry.get("description") or gen_name)[:1500],
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "args": {
                                    "type": "object",
                                    "description": "Skill-specific arguments",
                                },
                            },
                            "required": [],
                        },
                    },
                })
                skill_map[fn_name] = {
                    "name": gen_name,
                    "method": str(entry.get("method") or "POST"),
                    "path": str(
                        entry.get("path") or f"/api/agent/generated-skill/{gen_name}"
                    ),
                    "gate_actions": entry.get("gate_actions") or [],
                }
        except Exception as exc:
            log_degraded("agent_loop.generated_capabilities", exc)

    _CAPABILITY_GATE_ACTIONS = gate_actions
    logger.info("capabilities_loaded", count=len(tools))
    return tools, skill_map


def load_registry(
    expose_filter: set[str] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Load skills from YAML registry (legacy mode — used by scenarios and fallback).

    In capabilities mode this is bypassed for the chat agent but still used
    by the scenario runner which needs direct endpoint access.

    Args:
        expose_filter: if given, only skills in this set are included.
                       Pass None to load ALL skills.
    Returns:
        (openai_tools_list, sanitized_name → skill_dict)
    """
    registry_path = gateway_config.registry_path
    if not registry_path.exists():
        logger.warning("skills_registry_not_found", path=str(registry_path))
        return [], {}

    data = yaml.safe_load(registry_path.read_text())
    skills: list[dict] = data.get("skills") or data.get("tools") or []

    tools: list[dict] = []
    skill_map: dict[str, dict] = {}

    for skill in skills:
        if expose_filter is not None and skill["name"] not in expose_filter:
            continue

        params_schema = skill.get("parameters") or {}
        path_params = re.findall(r"\{(\w+)\}", skill.get("path", ""))

        properties: dict[str, Any] = {}
        required: list[str] = []

        for pp in path_params:
            properties[pp] = {"type": "string", "description": f"ID: {pp}"}
            required.append(pp)

        if params_schema.get("properties"):
            for k, v in params_schema["properties"].items():
                if k not in properties:
                    properties[k] = {kk: vv for kk, vv in v.items() if kk not in ("title",)}

        if params_schema.get("required"):
            for r in params_schema["required"]:
                if r not in required:
                    required.append(r)

        _type_map = {"string": "string", "str": "string", "int": "integer",
                     "integer": "integer", "float": "number", "number": "number",
                     "bool": "boolean", "boolean": "boolean", "object": "object",
                     "array": "array", "list": "array"}
        for param in (skill.get("body_params") or []) + (skill.get("query_params") or []):
            if not isinstance(param, dict):
                continue
            pname = param.get("name", "")
            if not pname or pname in properties:
                continue
            ptype = _type_map.get(str(param.get("type", "string")).lower(), "string")
            prop: dict[str, Any] = {"type": ptype}
            if param.get("description"):
                prop["description"] = param["description"]
            if ptype == "array":
                prop["items"] = {"type": "object"}
            properties[pname] = prop
            if param.get("required"):
                required.append(pname)

        fn_name = sanitize_name(skill["name"])
        tools.append({
            "type": "function",
            "function": {
                "name": fn_name,
                "description": skill.get("description", skill["name"])[:200],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
        skill_map[fn_name] = skill

    return tools, skill_map


def _load_agent_skills(
    expose_filter: set[str] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Load skills for the chat agent.

    In capabilities mode: loads capabilities.yml (15 broad tools).
    In registry mode: loads _registry.yml filtered by expose_filter.
    """
    if gateway_config.skills_mode == "capabilities":
        return _load_capabilities()
    return load_registry(expose_filter)


_RU_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)
_RU_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _today_context() -> str:
    """Live 'today is <date>' line for the system prompt — see _effective_system."""
    from datetime import datetime

    now = datetime.now().astimezone()
    weekday = _RU_WEEKDAYS[now.weekday()]
    month = _RU_MONTHS_GENITIVE[now.month - 1]
    return (
        f"## Текущая дата и время\n"
        f"Сегодня {now.day} {month} {now.year} г., {weekday}, "
        f"{now.strftime('%H:%M')} ({now.strftime('%Z') or 'локальное время сервера'}). "
        f"ISO: {now.strftime('%Y-%m-%d')}. Все относительные даты («завтра», «через неделю», "
        f"«в следующий понедельник») считай от этой даты, а не по памяти."
    )


# Ф7 (AGENT_AUTONOMY_ROADMAP.md): pure wording/register guidance — must never
# be phrased as instructions that could bias which action/decision the model
# reaches, only how the resulting text reads. "neutral" (the default) adds no
# suffix at all, so nobody who has never touched this setting sees any prompt
# change from before Ф7 existed.
_TONE_STYLE_HINTS: dict[str, str] = {
    "friendly": (
        "## Стиль ответа\n"
        "Отвечай тепло и по-человечески, обращайся на «вы», без сухого "
        "канцелярита — как коллега, а не отчёт."
    ),
    "formal": (
        "## Стиль ответа\n"
        "Отвечай официально и по-деловому: точные формулировки, без "
        "разговорных оборотов и эмодзи."
    ),
    "concise": (
        "## Стиль ответа\n"
        "Отвечай предельно кратко — только суть, без вводных фраз и "
        "повторов вопроса."
    ),
}


def _tone_style_hint(tone: str) -> str | None:
    return _TONE_STYLE_HINTS.get(tone)


def _load_system_prompt(config: BuiltinAgentConfig | None = None) -> str:
    if config and config.system_prompt:
        return f"{config.system_prompt.strip()}\n\n{_OPERATIONAL_POLICY}"
    path = gateway_config.base_prompt_path
    if path.exists():
        raw = path.read_text()
        base_prompt = raw.replace(
            "[ИНСТРУМЕНТЫ ЗАГРУЖАЮТСЯ АВТОМАТИЧЕСКИ ИЗ РЕЕСТРА SKILLS]", ""
        ).strip()
        return f"{base_prompt}\n\n{_OPERATIONAL_POLICY}"
    agent_name = config.agent_name if config else gateway_config.agent_name
    return (
        f"Ты — AI-ассистент производственного предприятия. Твоё имя: {agent_name}.\n\n"
        f"{_OPERATIONAL_POLICY}"
    )


# ── HTTP skill executor ───────────────────────────────────────────────────────

async def execute_skill(
    skill: dict,
    args: dict,
    config: BuiltinAgentConfig,
    *,
    approval_granted: bool = False,
) -> dict:
    # MCP-derived skill entries (built-in or external-server tools loaded by
    # _init_mcp/load_mcp_tools) carry a direct async handler instead of an
    # HTTP method/path — call it in-process. Without this branch every MCP
    # tool call KeyErrors on skill["method"] the first time it actually runs
    # (the tool schema/gate wiring worked; only invocation was missing).
    if skill.get("_method") in {"mcp", "builtin"} and callable(skill.get("_handler")):
        try:
            return await skill["_handler"](args)
        except Exception as exc:
            return {"error": str(exc)}

    method = skill["method"].upper()
    path = skill["path"]
    base_url = config.backend_url.rstrip("/")
    timeout = config.backend_timeout_seconds
    # Web research/browse open many live pages (+ PDF OCR) and legitimately run
    # for minutes. The default 30s would time out and trigger retries that
    # re-run the whole search. Give these calls a generous budget.
    _action = str(args.get("action") or "").lower()
    if path.endswith("/cap/search") and _action in {"research", "browse", "web"}:
        timeout = max(float(timeout), 300.0)
    elif "/web-search/" in path:
        timeout = max(float(timeout), 300.0)

    path_params = set(re.findall(r"\{(\w+)\}", path))
    body_args: dict = {}
    query_args: dict = {}

    for k, v in args.items():
        if k in path_params:
            path = path.replace(f"{{{k}}}", str(v))
        elif method == "GET":
            query_args[k] = v
        else:
            body_args[k] = v

    url = base_url + path
    max_retries = 3
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            _hdrs = internal_headers()
            if approval_granted:
                _hdrs["X-Agent-Approval"] = "granted"
                # Привязываем одобрение к КОНКРЕТНЫМ аргументам вызова.
                # Голое "granted" сообщало границе лишь «цикл что-то одобрил»:
                # заголовок годился для любого другого вызова той же
                # capability. Долговечный путь (tasks/work_orders) так делает
                # давно — чат-путь оставался единственным без привязки.
                _hdrs["X-Agent-Approval-Digest"] = _capability_args_digest(
                    query_args if method == "GET" else body_args
                )
            async with httpx.AsyncClient(timeout=float(timeout)) as client:
                if method == "GET":
                    resp = await client.get(url, params=query_args, headers=_hdrs)
                elif method == "POST":
                    resp = await client.post(url, json=body_args, headers=_hdrs)
                elif method == "PATCH":
                    resp = await client.patch(url, json=body_args, headers=_hdrs)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=_hdrs)
                else:
                    return {"error": f"Unsupported method: {method}"}

            if resp.status_code < 400:
                try:
                    return resp.json()
                except Exception:
                    return {"text": resp.text[:2000]}
            elif resp.status_code in {502, 503, 504} and attempt < max_retries - 1:
                last_error = Exception(f"HTTP {resp.status_code}")
                await asyncio.sleep(2 ** attempt)
                continue
            else:
                # Surface structured dispatcher errors (error_code + available/
                # missing) so the model can self-correct, instead of a raw,
                # truncated HTTP-text blob it cannot reliably parse.
                try:
                    body = resp.json()
                    detail = body.get("detail") if isinstance(body, dict) else None
                except Exception:
                    detail = None
                if isinstance(detail, dict) and detail.get("error_code"):
                    return {"status": f"HTTP {resp.status_code}", **detail}
                return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:300]}

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = e
            logger.warning(
                "skill_http_retry",
                skill=skill.get("name"),
                attempt=attempt + 1,
                error=str(e),
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"Skill execution failed after {max_retries} attempts: {last_error}"}


# ── Ollama client (streaming) ─────────────────────────────────────────────────

def _merge_system_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Fold any inline ``system``-role entries into one leading system message.

    ``AgentSession.inject_orchestrator_hint`` (and other mid-turn hints) stash
    their text as a ``system``-role entry inside the rolling ``self.messages``
    history, appended *after* prior user/assistant turns. Recent Ollama builds
    (0.32.x+) strictly validate chat-template role ordering and reject the
    request with ``system message must be at the beginning`` whenever a system
    message shows up anywhere but index 0 — which broke virtually every
    worker-dispatched turn. Mirrors the merge `_convert_messages_to_anthropic`
    already does for the Anthropic path.
    """
    extra_system = [m.get("content", "") for m in messages if m.get("role") == "system" and m.get("content")]
    rest = [m for m in messages if m.get("role") != "system"]
    merged_prompt = "\n\n".join([system_prompt, *extra_system]) if extra_system else system_prompt
    return [{"role": "system", "content": merged_prompt}] + rest


async def _call_ollama_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    model_override: str | None = None,
    disable_thinking: bool | None = None,
    thinking_level: str | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Stream Ollama response; calls on_token for each text chunk."""
    model = _get_agent_model(config, model_override=model_override)
    ollama_url = config.ollama_url.rstrip("/")
    options: dict[str, Any] = {"temperature": config.temperature}
    if max_tokens:
        options["num_predict"] = int(max_tokens)
    payload = {
        "model": model,
        "messages": _merge_system_messages(system_prompt, messages),
        "tools": tools,
        "stream": True,
        "options": options,
    }
    disabled = _thinking_disabled(config, disable_thinking, model_name=model)
    if disabled:
        payload["think"] = False
    else:
        # A level is only sent when the model's catalog entry declares
        # thinking_levels — otherwise think is left unset (prior behaviour:
        # rely on the model/server default rather than force True).
        level = _thinking_level(
            config, disabled=False, level_override=thinking_level, model_name=model
        )
        if level:
            payload["think"] = level

    full_content = ""
    final_message: dict = {}
    accumulated_tool_calls: list | None = None
    scrubber = StreamingContextScrubber()

    async with httpx.AsyncClient(timeout=float(config.llm_timeout_seconds)) as client:
        async with client.stream(
            "POST", f"{ollama_url}/api/chat", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = chunk.get("message", {})
                token: str = msg.get("content", "") or ""

                if msg.get("tool_calls"):
                    accumulated_tool_calls = msg["tool_calls"]

                visible = scrubber.feed(token)
                if visible:
                    full_content += visible
                    await on_token(visible)

                if chunk.get("done"):
                    trailing = scrubber.flush()
                    if trailing:
                        full_content += trailing
                        await on_token(trailing)
                    final_message = msg
                    final_message["content"] = full_content
                    if accumulated_tool_calls and not final_message.get("tool_calls"):
                        final_message["tool_calls"] = accumulated_tool_calls
                    # Б15: Ollama's final streaming chunk carries token counts as
                    # documented top-level fields (sibling to "message", not
                    # nested in it) — cheap to read, previously dropped
                    # entirely. Only Ollama is captured in this pass; the
                    # OpenAI-compatible and Anthropic streaming paths need
                    # their own (differently-shaped) usage parsing, deferred —
                    # see AGENT_SYSTEM_REMEDIATION_PLAN.md Б15.
                    if "prompt_eval_count" in chunk or "eval_count" in chunk:
                        final_message["_usage"] = {
                            "input_tokens": int(chunk.get("prompt_eval_count") or 0),
                            "output_tokens": int(chunk.get("eval_count") or 0),
                        }
                    break

    return final_message


# ── OpenAI-compatible streaming (OpenRouter / DeepSeek) ──────────────────────

def _openai_compatible_provider_config(
    provider: str,
    config: BuiltinAgentConfig,
) -> tuple[str, str, dict[str, str]]:
    mapping: dict[str, tuple[str, str, dict[str, str]]] = {
        "openrouter": (
            "https://openrouter.ai/api/v1",
            "OPENROUTER_API_KEY",
            {
                "HTTP-Referer": "https://ai-workspace.local",
                "X-Title": "AI Manufacturing Workspace",
            },
        ),
        "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", {}),
        "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", {}),
        "gemini": (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "GEMINI_API_KEY",
            {},
        ),
        "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEY", {}),
        "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", {}),
        "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY", {}),
        "fireworks": ("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY", {}),
        "xai": ("https://api.x.ai/v1", "XAI_API_KEY", {}),
        "cohere": ("https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY", {}),
        "perplexity": ("https://api.perplexity.ai", "PERPLEXITY_API_KEY", {}),
        "minimax": ("https://api.minimax.io/v1", "MINIMAX_API_KEY", {}),
        "kimi": ("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", {}),
        "qwen": (
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "DASHSCOPE_API_KEY",
            {},
        ),
    }
    local_mapping: dict[str, tuple[str, str, dict[str, str]]] = {
        # llamacpp: llama-server speaks OpenAI-compatible API at /v1/*
        # settings.llamacpp_url = http://llama-server:8080 (Docker) — must append /v1
        "llamacpp": (_settings.llamacpp_url.rstrip("/") + "/v1", "", {}),
        "vllm": (config.vllm_url.rstrip("/"), "VLLM_API_KEY", {}),
        "lmstudio": (config.lmstudio_url.rstrip("/"), "LMSTUDIO_API_KEY", {}),
        "openai_compatible": (
            config.openai_compatible_url.rstrip("/"),
            "OPENAI_COMPATIBLE_API_KEY",
            {},
        ),
    }
    if provider in {**mapping, **local_mapping}:
        base_url, env_key, extra = {**mapping, **local_mapping}[provider]
        return base_url, os.environ.get(env_key, ""), extra
    # Any other provider kind registered in model_registry.yaml — resolve its
    # endpoint and (DB-stored or env) API key through the provider registry.
    try:
        from app.ai.provider_registry import select_instance
        from app.ai.schemas import ProviderKind

        resolved = select_instance(ProviderKind(provider))
        if resolved.base_url:
            return resolved.base_url, resolved.api_key, {}
    except Exception:
        pass
    raise ValueError(f"Unsupported openai-compatible provider: {provider}")


# How each provider family controls chain-of-thought (from their API docs).
#   reasoning_effort:"none"|"low"|"medium"|"high" — Ollama (local+cloud),
#                             OpenAI o-series, Groq, xAI, DashScope/Qwen,
#                             Cerebras (OpenAI-compat surface).
#   reasoning:{enabled,effort} — OpenRouter extension.
#   chat_template_kwargs — llama.cpp/vLLM (Qwen3 template, binary only).
# Providers without a documented knob get nothing (avoid 400 on strict endpoints).
# _REASONING_EFFORT_PROVIDERS is imported at module top from
# app.ai.thinking_params — the single source of truth shared with
# providers/openai_compatible.py.


def _provider_instance_extra(provider: str) -> dict[str, Any]:
    """Return the provider node's UI-configured extra ({headers, body})."""
    try:
        from app.ai.provider_registry import select_instance
        from app.ai.schemas import ProviderKind

        return select_instance(ProviderKind(provider)).extra or {}
    except Exception:
        return {}


def _reasoning_disable_params(provider: str) -> dict[str, Any]:
    """Hard-off CoT params for a provider family. Kept as a thin, stable
    wrapper (existing tests call it directly) delegating to the shared
    ``thinking_request_params`` in app.ai.thinking_params.
    """
    return _thinking_request_params(provider, False, None)


def _reasoning_params(provider: str, thinking: bool, level: str | None) -> dict[str, Any]:
    """CoT params for a provider family, on/off + optional reasoning-effort
    level. Ollama and llama.cpp both serve Qwen3-family templates over the
    OpenAI-compatible endpoint; the CoT switch is the template kwarg
    ``enable_thinking`` (Ollama also accepts ``think``). Without this the
    tool-call path left thinking ON even when the role had it disabled, so the
    model wrapped its tool args in <think>… and mangled the JSON.
    """
    return _thinking_request_params(provider, thinking, level)


def _normalize_openai_messages(messages: list[dict]) -> list[dict]:
    """Make a message list strictly OpenAI-compliant for cloud endpoints.

    The OpenAI spec requires ``tool_calls[].function.arguments`` to be a JSON
    **string**. The agent carries it as a dict (Ollama's native format), which
    lenient gateways accept but strict ones (Ollama Cloud, OpenAI) reject with
    ``cannot unmarshal object … of type string``. Serialise any dict/list args.
    """
    out: list[dict] = []
    for msg in messages:
        calls = msg.get("tool_calls")
        if not calls:
            out.append(msg)
            continue
        new_msg = dict(msg)
        new_calls = []
        for call in calls:
            call = dict(call)
            fn = dict(call.get("function") or {})
            args = fn.get("arguments")
            if isinstance(args, (dict, list)):
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
            elif args is None:
                fn["arguments"] = "{}"
            call["function"] = fn
            new_calls.append(call)
        new_msg["tool_calls"] = new_calls
        out.append(new_msg)
    return out


async def _call_openai_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    provider: str,
    model_override: str | None = None,
    disable_thinking: bool | None = None,
    thinking_level: str | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Stream an OpenAI-compatible SSE endpoint (OpenRouter, DeepSeek).

    Returns a normalised message dict identical to the Ollama format so that
    the rest of AgentSession._run() needs no changes.

    ``on_thinking`` — optional async callback invoked with each ``reasoning_content``
    chunk emitted by thinking models (Qwen3, DeepSeek-R1, etc.).  Use it to send
    keepalive / status frames through the WebSocket so idle-connection timeouts
    (Traefik default ~180 s) do not drop the connection during long think phases.
    """
    model = _get_agent_model(config, model_override=model_override)
    base_url, api_key, extra = _openai_compatible_provider_config(provider, config)

    headers = {"Content-Type": "application/json", **extra}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": _normalize_openai_messages(
            _merge_system_messages(system_prompt, messages)
        ),
        "stream": True,
        "temperature": config.temperature,
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = tools
    _disabled = _thinking_disabled(config, disable_thinking, model_name=model)
    # The knob to control reasoning is provider-specific (per their docs).
    # Sending the wrong one to a strict endpoint returns 400, so dispatch by
    # provider family and stay silent for providers without a known knob.
    if _disabled:
        payload.update(_reasoning_params(provider, False, None))
    else:
        _level = _thinking_level(
            config, disabled=False, level_override=thinking_level, model_name=model
        )
        if _level:
            payload.update(_reasoning_params(provider, True, _level))

    # Per-provider extra headers / body params configured in the UI
    # (provider_instances.extra = {headers:{...}, body:{...}}).
    inst_extra = _provider_instance_extra(provider)
    headers.update(inst_extra.get("headers") or {})
    payload.update(inst_extra.get("body") or {})

    full_content = ""
    scrubber = StreamingContextScrubber()
    # Accumulate streamed tool calls: index → {id, name, arguments}
    tool_acc: dict[int, dict[str, str]] = {}

    async with httpx.AsyncClient(timeout=float(config.llm_timeout_seconds)) as client:
        async with client.stream(
            "POST", f"{base_url}/chat/completions", headers=headers, json=payload
        ) as resp:
            if resp.status_code >= 400:
                # Surface the provider's actual error body (e.g. "model requires a
                # subscription", "unknown parameter") instead of a bare 4xx/5xx.
                body = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise RuntimeError(
                    f"{provider} ({model}) → HTTP {resp.status_code}: {body}"
                )
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta", {})

                token: str = delta.get("content") or ""
                visible = scrubber.feed(token)
                if visible:
                    full_content += visible
                    await on_token(visible)

                # reasoning_content (thinking phase) — forward to on_thinking callback
                # so the caller can send keepalive/status frames during long think phases.
                thinking_token: str = delta.get("reasoning_content") or ""
                if thinking_token and on_thinking:
                    await on_thinking(thinking_token)

                for tc_delta in delta.get("tool_calls") or []:
                    idx: int = tc_delta.get("index", 0)
                    if idx not in tool_acc:
                        tool_acc[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.get("id"):
                        tool_acc[idx]["id"] = tc_delta["id"]
                    fn = tc_delta.get("function", {})
                    if fn.get("name"):
                        tool_acc[idx]["name"] += fn["name"]
                    if fn.get("arguments"):
                        tool_acc[idx]["arguments"] += fn["arguments"]

    trailing = scrubber.flush()
    if trailing:
        full_content += trailing
        await on_token(trailing)

    normalized_tool_calls = []
    for idx in sorted(tool_acc.keys()):
        tc = tool_acc[idx]
        try:
            args: Any = json.loads(tc["arguments"]) if tc["arguments"] else {}
        except json.JSONDecodeError:
            args = {}
        normalized_tool_calls.append({
            "type": "function",  # Required by llama.cpp when replaying tool-call history
            "id": tc["id"],
            "function": {"name": tc["name"], "arguments": args},
        })

    return {
        "role": "assistant",
        "content": full_content,
        "tool_calls": normalized_tool_calls or None,
    }


# ── Anthropic streaming ───────────────────────────────────────────────────────

def _convert_messages_to_anthropic(
    messages: list[dict],
    system_prompt: str,
) -> tuple[str, list[dict]]:
    """Convert OpenAI/Ollama-format messages to Anthropic Messages API format.

    Returns ``(system_text, anthropic_messages_list)``.
    """
    system_parts = [system_prompt] if system_prompt else []
    result: list[dict] = []
    pending_ids: list[str] = []
    pending_results: list[dict] = []

    def _flush() -> None:
        if pending_results:
            result.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()
            pending_ids.clear()

    for msg in messages:
        role = msg.get("role", "")
        content: str = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls") or []

        if role == "system":
            system_parts.append(content)
            continue

        if role in ("user", "assistant") and pending_results:
            _flush()

        if role == "user":
            result.append({"role": "user", "content": content})

        elif role == "assistant":
            if tool_calls:
                blocks: list[dict] = []
                if content:
                    blocks.append({"type": "text", "text": content})
                for i, tc in enumerate(tool_calls):
                    fn = tc.get("function", {})
                    name = fn.get("name", "unknown")
                    raw_args = fn.get("arguments", {})
                    args_dict = (
                        raw_args
                        if isinstance(raw_args, dict)
                        else json.loads(raw_args or "{}")
                    )
                    tc_id = tc.get("id") or f"toolu_{name}_{i}"
                    pending_ids.append(tc_id)
                    blocks.append({
                        "type": "tool_use",
                        "id": tc_id,
                        "name": name,
                        "input": args_dict,
                    })
                result.append({"role": "assistant", "content": blocks})
            elif content:
                result.append({"role": "assistant", "content": content})

        elif role == "tool":
            # Link result to its call by id when available; fall back to FIFO
            # order only for legacy messages that carry no tool_call_id.
            msg_id = msg.get("tool_call_id") or ""
            if msg_id and msg_id in pending_ids:
                pending_ids.remove(msg_id)
                tc_id = msg_id
            elif pending_ids:
                tc_id = pending_ids.pop(0)
            else:
                tc_id = msg_id or f"toolu_unknown_{len(pending_results)}"
            pending_results.append({
                "type": "tool_result",
                "tool_use_id": tc_id,
                "content": content,
            })

    _flush()
    return "\n\n".join(p for p in system_parts if p), result


def _convert_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    result = []
    for t in tools:
        fn = t.get("function", {})
        result.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


async def _call_anthropic_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    max_tokens: int | None = None,
) -> dict:
    """Stream Anthropic Messages API response; normalises output to Ollama format."""
    from app.config import settings

    api_key = os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key
    model = _get_agent_model(config)

    system_text, anthropic_msgs = _convert_messages_to_anthropic(messages, system_prompt)
    anthropic_tools = _convert_tools_to_anthropic(tools) if tools else []

    system_payload: Any = system_text
    if config.prompt_cache_enabled and system_text:
        system_payload = [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"},
        }]

    payload: dict[str, Any] = {
        "model": model,
        "messages": anthropic_msgs,
        "max_tokens": int(max_tokens) if max_tokens else 4096,
        "stream": True,
    }
    if system_text:
        payload["system"] = system_payload
    if anthropic_tools:
        payload["tools"] = anthropic_tools

    headers: dict[str, str] = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "accept": "text/event-stream",
    }
    if config.prompt_cache_enabled:
        headers["anthropic-beta"] = "prompt-caching-2024-07-31"

    full_content = ""
    scrubber = StreamingContextScrubber()
    # Accumulate tool_use blocks: index → {id, name, input_json}
    tool_acc: dict[int, dict[str, str]] = {}
    current_idx: int = 0

    async with httpx.AsyncClient(timeout=float(config.llm_timeout_seconds)) as client:
        async with client.stream(
            "POST", "https://api.anthropic.com/v1/messages", headers=headers, json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "content_block_start":
                    current_idx = event.get("index", 0)
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_acc[current_idx] = {
                            "id": block.get("id", f"toolu_{current_idx}"),
                            "name": block.get("name", ""),
                            "input_json": "",
                        }

                elif etype == "content_block_delta":
                    idx = event.get("index", current_idx)
                    delta = event.get("delta", {})
                    dtype = delta.get("type", "")

                    if dtype == "text_delta":
                        token = delta.get("text", "")
                        visible = scrubber.feed(token)
                        if visible:
                            full_content += visible
                            await on_token(visible)
                    elif dtype == "input_json_delta" and idx in tool_acc:
                        tool_acc[idx]["input_json"] += delta.get("partial_json", "")

    trailing = scrubber.flush()
    if trailing:
        full_content += trailing
        await on_token(trailing)

    normalized_tool_calls = []
    for idx in sorted(tool_acc.keys()):
        tc = tool_acc[idx]
        try:
            args_dict: Any = json.loads(tc["input_json"]) if tc["input_json"] else {}
        except json.JSONDecodeError:
            args_dict = {}
        normalized_tool_calls.append({
            "id": tc["id"],
            "function": {"name": tc["name"], "arguments": args_dict},
        })

    return {
        "role": "assistant",
        "content": full_content,
        "tool_calls": normalized_tool_calls or None,
    }


# ── Provider dispatcher ───────────────────────────────────────────────────────

_OPENAI_COMPATIBLE_PROVIDERS = frozenset({
    # local OpenAI-compatible servers
    "vllm",
    "lmstudio",
    "openai_compatible",
    "llamacpp",
    # cloud OpenAI-compatible gateways (must match ProviderKind values)
    "openrouter",
    "deepseek",
    "openai",
    "gemini",
    "ollama_cloud",
    "moonshot",       # Kimi
    "minimax",
    "dashscope",      # Qwen (Alibaba)
    "mistral",
    "groq",
    "together",
    "fireworks",
    "xai",
    "cohere",
    "perplexity",
    "deepinfra",
    "cerebras",
    "sambanova",
    "nebius",
    "novita",
    "hyperbolic",
    # legacy aliases
    "kimi",
    "qwen",
})

async def _call_provider_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str | None,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    model_override: str | None = None,
    provider_override: str | None = None,
    disable_thinking_override: bool | None = None,
    thinking_level_override: str | None = None,
    on_thinking: Callable[[str], Awaitable[None]] | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Dispatch to the configured LLM provider with optional fallback chain."""
    primary_provider = _get_agent_provider(config, provider_override=provider_override)
    providers_to_try = [primary_provider] + [
        provider
        for provider in list(config.fallback_providers or [])
        if provider != primary_provider
    ]

    last_exc: Exception | None = None
    transient_errors = (
        httpx.RemoteProtocolError,
        httpx.ReadError,
        httpx.ConnectError,
        httpx.PoolTimeout,
    )
    for p in providers_to_try:
        attempts = 2 if p == "ollama" else 1
        for attempt in range(1, attempts + 1):
            try:
                if p == "ollama":
                    return await _call_ollama_streaming(
                        messages,
                        tools,
                        system_prompt,
                        config,
                        on_token,
                        model_override=model_override,
                        disable_thinking=disable_thinking_override,
                        thinking_level=thinking_level_override,
                        max_tokens=max_tokens,
                    )
                elif p in _OPENAI_COMPATIBLE_PROVIDERS:
                    return await _call_openai_streaming(
                        messages,
                        tools,
                        system_prompt,
                        config,
                        on_token,
                        provider=p,
                        model_override=model_override,
                        disable_thinking=disable_thinking_override,
                        thinking_level=thinking_level_override,
                        on_thinking=on_thinking,
                        max_tokens=max_tokens,
                    )
                elif p == "anthropic":
                    return await _call_anthropic_streaming(
                        messages, tools, system_prompt, config, on_token,
                        max_tokens=max_tokens,
                    )
                else:
                    logger.warning("unknown_provider_falling_back", provider=p)
                    return await _call_ollama_streaming(
                        messages,
                        tools,
                        system_prompt,
                        config,
                        on_token,
                        model_override=model_override,
                        disable_thinking=disable_thinking_override,
                        thinking_level=thinking_level_override,
                        max_tokens=max_tokens,
                    )
            except transient_errors as exc:
                last_exc = exc
                logger.warning(
                    "provider_transient_error",
                    provider=p,
                    attempt=attempt,
                    attempts=attempts,
                    error=str(exc),
                )
                if attempt < attempts:
                    await asyncio.sleep(0.75 * attempt)
                    continue
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "provider_call_error",
                    provider=p,
                    model=model_override or config.worker_model,
                    url=getattr(config, "ollama_url", None) if p == "ollama" else None,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                break
        logger.warning(
            "provider_call_failed_trying_fallback",
            provider=p,
            model=model_override or config.worker_model,
            error=str(last_exc),
        )

    raise last_exc or RuntimeError("All configured providers failed")


# ── Agent session ─────────────────────────────────────────────────────────────

SendFn = Callable[[dict], Awaitable[None]]


class AgentSession:
    """Per-WebSocket-connection agent state.

    Skills and approval gates are read from gateway_config on session creation,
    so changes to gateway.yml take effect on the next conversation.
    """

    def __init__(self, send: SendFn) -> None:
        self._send = send
        self.messages: list[dict] = []
        self._approval_future: asyncio.Future[bool] | None = None
        # Правки, внесённые человеком в карточке подтверждения (см. on_approval).
        self._pending_args_override: dict | None = None
        # Durable-запись Approval, которую ждёт текущий ход (deliver_external_approval).
        self._pending_db_id: str | None = None
        # Что человек уже одобрил в этом ходе (см. _approval_key). Очищается
        # при каждом новом сообщении пользователя: одобрение действует на ход,
        # а не навсегда.
        self._granted_approvals: set[str] = set()
        self._session_id = str(uuid.uuid4())
        self._iteration = 0
        # Role-specific system prompt fragment, set per turn by the orchestrator.
        # Replaced (not accumulated) each turn so it never bloats history.
        self._role_context: str = ""
        # Per-turn response token budget, set by the orchestrator from task tier.
        # Keeps simple answers cheap (fast on local models) and lets reports grow.
        self._response_budget: int = 2048
        # Per-turn worker model override, set by the orchestrator from task tier
        # (e.g. a small fast model for simple turns). None → use configured model.
        self._turn_model_override: str | None = None
        # Worker role for the current turn — scopes the visible tool set to the
        # role's capability allowlist from gateway.yml. None → no scoping.
        self._active_role: str | None = None
        # Per-turn hard tool exclusions (set by the orchestrator). Used to keep
        # the worker off slow RAG tools (memory/search/documents) when the task
        # is structured-data only (e.g. a spec_table). Reset each turn.
        self._excluded_tools: set[str] = set()
        self._recommended_capabilities: set[str] = set()
        # Orchestrator routed this turn to the desktop — reliable auto-publish
        # fallback in _deliver_final_content (by intent, not keyword). Reset each turn.
        self._workspace_expected: bool = False

        self._config = get_builtin_agent_config()
        self._rebuild_runtime_components(self._config)
        self._mcp_initialised = False
        # Б15: summed across every LLM call this session makes, from the
        # "_usage" key _call_ollama_streaming attaches to its returned
        # message (see _accumulate_usage). Only Ollama calls are counted in
        # this pass — see the note at "_usage" for why. Public: WorkOrder's
        # headless runner (agent_cron.run_headless_agent_turn) reads this
        # after the turn to report tokens_used on the WorkStepAttempt.
        self.total_tokens: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._registry_mtime: float = _registry_mtime()
        _ACTIVE_SESSIONS.add(self)

    def reload_skills(self) -> None:
        """Hot-reload skill map from registry — used by CapabilityBuilder after new skill creation."""
        exposed = set(self._config.exposed_skills)
        self._tools, self._skill_map = _load_agent_skills(expose_filter=exposed if exposed else None)
        self._registry_mtime = _registry_mtime()
        logger.info(
            "agent_session_skills_reloaded",
            session_id=self._session_id,
            skill_count=len(self._skill_map),
        )

    async def _call_for_compression(
        self,
        messages: list[dict],
        model: str,
        tools: list[dict],
    ) -> Any:
        """Async generator adapter used by ContextCompressor for summarisation calls."""
        config = self._config
        accumulated: list[str] = []

        async def _collect(token: str) -> None:
            accumulated.append(token)

        await _call_provider_streaming(
            messages,
            [],
            None,
            config,
            _collect,
            model_override=model,
            provider_override=config.worker_provider,
            disable_thinking_override=config.worker_disable_thinking,
            thinking_level_override=config.worker_thinking_level,
        )
        for chunk in accumulated:
            yield chunk

    async def _log_action(self, **kwargs: Any) -> None:
        """Persist agent step to DB (fire-and-forget)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self._config.backend_url.rstrip('/')}/api/agent-actions",
                    json={"session_id": self._session_id, **kwargs},
                    headers=internal_headers(),
                )
        except Exception as exc:
            log_degraded("agent_loop.action_log", exc)

    def _accumulate_usage(self, message: dict | None) -> None:
        """Fold one LLM call's token usage (if any) into self.total_tokens.

        No-op when the provider didn't attach "_usage" (Б15 covers Ollama
        only in this pass) — total_tokens then stays {0, 0}, an honest
        "unknown", not a wrong number.
        """
        usage = (message or {}).get("_usage")
        if not isinstance(usage, dict):
            return
        self.total_tokens["input_tokens"] += int(usage.get("input_tokens") or 0)
        self.total_tokens["output_tokens"] += int(usage.get("output_tokens") or 0)

    async def _init_mcp(self) -> None:
        """Lazy-init MCP tools on first message (async-safe)."""
        if self._mcp_initialised:
            return
        self._mcp_initialised = True
        servers = self._config.mcp_servers or []
        if not servers:
            return
        try:
            from app.ai.mcp_client import load_mcp_tools
            mcp_tools, mcp_handlers = await load_mcp_tools(servers)
            self._tools.extend(mcp_tools)
            self._skill_map.update(mcp_handlers)
            if mcp_tools:
                logger.info("mcp_tools_loaded", count=len(mcp_tools))
        except Exception as exc:
            logger.warning("mcp_init_failed", error=str(exc))
            await self._send({
                "type": "system_warning",
                "code": "mcp_init_failed",
                "message": f"MCP инструменты не загружены: {exc}. Инструменты MCP недоступны в этой сессии.",
            })

    def _rebuild_runtime_components(self, config: BuiltinAgentConfig) -> None:
        """Rebuild tools/system/dependencies when runtime agent config changes."""
        self._config = config
        exposed = set(self._config.exposed_skills)
        self._tools, self._skill_map = _load_agent_skills(expose_filter=exposed if exposed else None)
        self._system = _load_system_prompt(self._config)
        self._approval_gates = set(self._config.approval_gates)
        self._pending_approval_id: str | None = None

        from app.ai.context_compressor import ContextCompressor
        self._compressor = ContextCompressor(
            model=_get_agent_model(self._config),
            threshold_percent=self._config.context_compression_threshold,
            compression_model=self._config.compression_model,
        ) if self._config.context_compression_enabled else None

        from app.ai.memory_manager import MemoryManager
        self._memory_mgr = MemoryManager(
            base_url=self._config.backend_url,
            headers=internal_headers(),
        )
        # Re-init MCP tools with updated server config on next message.
        self._mcp_initialised = False

    def _refresh_runtime_config(self) -> None:
        """Reload config and skill registry when either changes, without reconnect."""
        latest_config = get_builtin_agent_config()
        config_changed = (
            latest_config.model_dump(mode="json") != self._config.model_dump(mode="json")
        )
        current_mtime = _registry_mtime()
        registry_changed = current_mtime != self._registry_mtime
        if not config_changed and not registry_changed:
            return
        self._rebuild_runtime_components(latest_config)
        self._registry_mtime = current_mtime
        logger.info(
            "agent_runtime_config_reloaded",
            model=_get_agent_model(self._config),
            provider=self._config.provider,
            exposed_skills=len(self._config.exposed_skills),
            registry_reloaded=registry_changed,
        )

    def hydrate_history(self, messages: list[dict[str, str]]) -> None:
        """Restore chat-local dialogue context from persisted messages."""
        self.messages = [
            {"role": msg["role"], "content": msg["content"]}
            for msg in messages
            if msg.get("role") in {"user", "assistant"} and msg.get("content")
        ]
        self._trim_history()

    def recent_dialogue(self, limit: int = 20) -> list[dict[str, str]]:
        """Recent user/assistant turns from the compression-aware message history.

        Single source of truth for dialogue context — the orchestrator planner
        uses this instead of its own list so both stay in sync as the executor
        compresses long conversations.
        """
        turns = [
            {"role": str(m.get("role")), "content": str(m.get("content") or "")}
            for m in self.messages
            if m.get("role") in {"user", "assistant"} and m.get("content")
        ]
        return turns[-limit:]

    def record_external_turn(self, user_text: str, assistant_text: str) -> None:
        """Record a turn answered outside the executor (secretary direct path).

        Keeps the dialogue history coherent for future planning and feeds the
        episodic memory exactly like a normal executor turn.
        """
        self.messages.append({"role": "user", "content": user_text})
        self.messages.append({"role": "assistant", "content": assistant_text})
        self._trim_history()
        self._remember_latest_turn(assistant_text)

    def inject_orchestrator_hint(self, hint: str) -> None:
        """Inject an orchestrator plan hint as a system message before the next user turn."""
        self.messages.append({"role": "system", "content": hint})

    def set_role_context(self, role_prompt: str | None) -> None:
        """Set the role-specific system prompt fragment for the next turn.

        Replaces the previous value rather than accumulating, so switching roles
        between turns never leaves stale role guidance in the system prompt.
        """
        self._role_context = (role_prompt or "").strip()

    def set_active_role(self, role: str | None) -> None:
        """Set the worker role for the next turn — scopes the visible tool set.

        Tools are filtered to the role's capability allowlist from gateway.yml
        (plus the always-available core: workspace, memory, search). A role
        without a declared allowlist sees every tool (back-compat).
        """
        self._active_role = (role or "").strip() or None

    def set_excluded_tools(self, names: set[str] | None) -> None:
        """Hard-hide these capabilities from the worker for the next turn.

        Overrides even the always-available core set — used by the orchestrator
        to keep structured-data turns (spec_table) off slow RAG tools. Reset
        (passed empty) each turn by the orchestrator.
        """
        self._excluded_tools = set(names or ())

    def set_recommended_capabilities(self, names: set[str] | None) -> None:
        """Capabilities the router/planner chose for this turn — always visible.

        The role allowlist scopes tools by JOB, but the turn router picks the
        tool by MEANING of the request; when the two disagreed the worker simply
        never saw the recommended capability and improvised with whatever was in
        the allowlist (live finding: "прикрепи каталог к поставщику" → the
        catalog tool was invisible, so the worker published a summary table
        instead). A capability the planner explicitly chose is in scope for that
        turn by construction. Reset each turn by the orchestrator; ``exclude``
        still wins (that gate is about cost, not scope).
        """
        self._recommended_capabilities = {
            str(n).strip() for n in (names or ()) if str(n).strip()
        }

    def set_response_budget(self, max_tokens: int) -> None:
        """Set the per-turn max response tokens (clamped to a sane range)."""
        self._response_budget = max(256, min(int(max_tokens), 16384))

    def set_workspace_expected(self, expected: bool) -> None:
        """Tell the worker the orchestrator routed this turn to the desktop.

        Used as a reliable fallback in ``_deliver_final_content``: a structural
        result is auto-published to the desktop by *intent*, not by keyword
        markers in the user's phrasing. Reset each turn by the orchestrator.
        """
        self._workspace_expected = bool(expected)

    def set_model_override(self, model: str | None) -> None:
        """Set a per-turn worker model (tier-based fast/strong routing).

        Replaced each turn. None → fall back to the configured worker/model.
        Does not affect builder turns (capability generation keeps builder_model).
        """
        self._turn_model_override = (model or "").strip() or None

    # Capabilities every role can always use, regardless of its allowlist.
    # sheets are a safe scratch area (no production DB writes) — available to all.
    _CORE_CAPABILITIES = frozenset({"workspace", "memory", "search", "sheets"})

    def _tools_for_turn(self) -> list[dict]:
        """Visible tools for the current turn, scoped by the active role.

        Only applies in capabilities mode (tool name == capability name).
        MCP tools and tools outside the capability registry pass through.
        A role with no declared allowlist sees the full set.
        """
        def _tool_name(tool: dict) -> str:
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            return str(fn.get("name") or "")

        def _apply_exclusions(tools: list[dict]) -> list[dict]:
            if not self._excluded_tools:
                return tools
            return [t for t in tools if _tool_name(t) not in self._excluded_tools]

        role = self._active_role
        if not role or gateway_config.skills_mode != "capabilities":
            return _apply_exclusions(self._tools)
        allowed = gateway_config.role_capabilities(role)
        if not allowed:
            return _apply_exclusions(self._tools)
        # Names of registry capabilities (excludes MCP tools, which pass through).
        capability_names = set(_load_capabilities()[1].keys())
        visible = set(allowed) | self._CORE_CAPABILITIES | self._recommended_capabilities

        return _apply_exclusions([
            tool
            for tool in self._tools
            if _tool_name(tool) not in capability_names or _tool_name(tool) in visible
        ])

    def _effective_system(self) -> str:
        """Base system prompt plus live date grounding plus the per-turn role
        context (if any).

        Found via live testing: nothing anywhere in this codebase told the
        model what today's date actually is. Relative-date tool arguments
        ("напомни завтра") are computed from the model's own training-time
        sense of "now" — confirmed wrong by a full year in a live test — not
        from the deployment's real clock. Recomputed on every call (not
        baked in once at session start) so a session spanning midnight still
        sees the correct day.

        Ф7 (AGENT_AUTONOMY_ROADMAP.md): also appends the agent_tone style
        hint (if any) — a suffix, same mechanism as role_context below, not
        a rewrite of the base prompt. Read fresh from self._config each call
        (not baked into self._system at _rebuild_runtime_components time)
        for the same reason _today_context is recomputed here rather than
        cached: a config change should take effect on the very next turn,
        not require a session restart.
        """
        system = f"{self._system}\n\n{_today_context()}"
        tone_hint = _tone_style_hint(getattr(self._config, "agent_tone", "neutral"))
        if tone_hint:
            system = f"{system}\n\n{tone_hint}"
        if self._role_context:
            return f"{system}\n\n## Роль в этой задаче\n{self._role_context}"
        return system

    async def on_user_message(self, content: str) -> None:
        self._refresh_runtime_config()
        await self._init_mcp()
        # Одобрение действует на ход, а не навсегда: новое сообщение человека
        # начинает новый ход, и прошлое «да» его не покрывает.
        self._granted_approvals.clear()
        self.messages.append({"role": "user", "content": content})
        self._trim_history()
        await self._run()

    async def _publish_canvas(
        self,
        block: dict[str, Any],
        *,
        canvas_id: str | None = None,
        append: bool = True,
    ) -> None:
        try:
            async with httpx.AsyncClient(
                timeout=float(self._config.backend_timeout_seconds)
            ) as client:
                await client.post(
                    f"{self._config.backend_url.rstrip('/')}/api/canvas/publish",
                    json={"canvas_id": canvas_id, "block": block, "append": append},
                    headers=internal_headers(),
                )
        except Exception as exc:
            log_degraded("agent_loop.canvas_publish", exc)
        await self._send({
            "type": "canvas",
            "canvas_id": canvas_id,
            "block": block,
            "append": append,
        })

    async def on_approval(
        self,
        approved: bool,
        approval_id: str | None = None,
        db_id: str | None = None,
        args_override: dict | None = None,
    ) -> None:
        if self._pending_approval_id and approval_id != self._pending_approval_id:
            await self._send({
                "type": "approval_ignored",
                "approval_id": approval_id,
                "message": "Approval decision does not match the active request.",
            })
            return
        # Человек поправил письмо прямо в карточке подтверждения: содержимое
        # изменилось, значит изменился и его отпечаток. Раньше единственным
        # способом поправить формулировку было отклонить, объяснить словами и
        # ждать нового черновика — полный круг генерации ради одной строки.
        if approved and args_override:
            self._pending_args_override = dict(args_override)
        if self._approval_future and not self._approval_future.done():
            self._approval_future.set_result(approved)

    async def request_confirmation(self, prompt: str, meta: dict | None = None) -> bool:
        """Ask the user a yes/no question, reusing the approval future channel.

        Lightweight (no DB approval row) — used by explainable recipe replay to
        confirm a learned shortcut before it has earned silent trust. Times out
        to False (defer to the normal path) so a missing user never blocks.
        """
        approval_id = str(uuid.uuid4())
        self._pending_approval_id = approval_id
        self._approval_future = asyncio.get_event_loop().create_future()
        await self._send({
            "type": "approval_request",
            "tool": "recipe_replay",
            "preview": prompt,
            "approval_id": approval_id,
            **(meta or {}),
        })
        try:
            return await asyncio.wait_for(
                self._approval_future,
                timeout=float(self._config.approval_timeout_seconds),
            )
        except (asyncio.TimeoutError, TimeoutError):
            self._approval_future = None
            return False
        finally:
            self._pending_approval_id = None

    def _remember_latest_turn(self, delivered_text: str) -> None:
        if not self._config.memory_enabled or not delivered_text:
            return
        latest_user = next(
            (
                m.get("content", "")
                for m in reversed(self.messages)
                if m.get("role") == "user"
            ),
            "",
        )
        asyncio.create_task(
            self._memory_mgr.sync_turn(
                str(latest_user or ""),
                delivered_text,
                session_id=self._session_id,
            )
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _inject_rating_hint(self) -> None:
        """Append tool preference hint from rating history to the system message."""
        try:
            from app.ai.orchestrator_memory import build_tool_preference_hint
            last_user = next(
                (str(m.get("content", "")) for m in reversed(self.messages) if m.get("role") == "user"),
                "",
            )
            if not last_user:
                return
            candidate_skills = list(self._skill_map.keys())
            hint = build_tool_preference_hint(last_user, "general", candidate_skills)
            if not hint:
                return
            # Find and update existing system message or append to first message
            for msg in self.messages:
                if msg.get("role") == "system":
                    if hint not in str(msg.get("content", "")):
                        msg["content"] = str(msg.get("content", "")) + f"\n\n{hint}"
                    return
        except Exception as exc:
            log_degraded("agent_loop.rating_hint", exc)

    async def _inject_learning_rules(self) -> None:
        """Inject active learned rules into the system prompt.

        Two kinds of active ``TechnologyLearningRule`` are consumed:
        - nomenclature rules (default) — domain field guidance, injected globally;
        - behavioural rules (``rule_type == "behavior"``) — corrections to how the
          agent should act, injected ONLY when relevant to the current request
          (matched on field_name / metadata.trigger_keywords) so the system
          prompt is not flooded with every rule on every turn.
        """
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                resp = await client.get(
                    f"{self._config.backend_url.rstrip('/')}/api/technology/learning-rules",
                    params={"status": "active", "limit": 50},
                )
                if resp.status_code != 200:
                    return
                rules: list[dict] = (resp.json() or {}).get("items") or []
            if not rules:
                return

            user_text = _normalize_ru_yo(
                next(
                    (str(m.get("content") or "") for m in reversed(self.messages)
                     if m.get("role") == "user"),
                    "",
                ).lower()
            )

            def _behavior_is_relevant(rule: dict) -> bool:
                # Triggers come from metadata.trigger_keywords. field_name is a
                # label/category, not a trigger. No triggers → global guidance.
                meta = rule.get("metadata") or rule.get("metadata_") or {}
                triggers = (
                    [str(t) for t in (meta.get("trigger_keywords") or [])]
                    if isinstance(meta, dict) else []
                )
                if not triggers:
                    return True
                return any(_normalize_ru_yo(t.lower()) in user_text for t in triggers if t)

            nomenclature: list[str] = []
            behavioural: list[str] = []
            for r in rules:
                obs = (r.get("replacement_value") or "").strip()
                if not obs:
                    continue
                if str(r.get("rule_type") or "") == "behavior":
                    if _behavior_is_relevant(r):
                        behavioural.append(f"- {obs}")
                else:
                    tool = (r.get("field_name") or "").strip()
                    nomenclature.append(f"- При использовании [{tool}]: {obs}")

            sections: list[str] = []
            if behavioural:
                sections.append(
                    "## Усвоенные поправки поведения (применимы к этому запросу):\n"
                    + "\n".join(behavioural[:10])
                )
            if nomenclature:
                sections.append(
                    "## Усвоенные правила номенклатуры:\n" + "\n".join(nomenclature[:20])
                )
            if not sections:
                return
            block = "\n\n".join(sections)
            for msg in self.messages:
                if msg.get("role") == "system":
                    if block not in str(msg.get("content", "")):
                        msg["content"] = str(msg.get("content", "")) + f"\n\n{block}"
                    return
        except Exception as exc:
            log_degraded("agent_loop.learning_rules", exc)

    async def _try_fast_intent(self) -> bool:
        """Deterministic fast-path for high-confidence count questions.

        Skips the whole LLM tool-calling loop (and memory/hint injections) for
        unambiguous "сколько X" queries — a pure speed win on weak local models.
        Returns True when the turn was fully handled. Generic: no hardcoded
        product categories (see ``fast_intent_router``).
        """
        from app.ai.fast_intent_router import match_fast_intent

        content = next(
            (
                str(m.get("content") or "")
                for m in reversed(self.messages)
                if m.get("role") == "user"
            ),
            "",
        )
        if not content:
            return False
        intent = match_fast_intent(content)
        if intent is None:
            return False
        skill = self._skill_map.get(intent.capability)
        if not skill:
            return False  # capability not exposed / registry mode → defer to LLM

        from app.ai.result_cache import cache_get, cache_set
        cache_key = f"{intent.capability}:{intent.action}:{intent.search_term or ''}"

        # Cache hit → instant answer, no backend round-trip.
        cached = cache_get(cache_key)
        if cached is not None:
            await self._send({"type": "text", "content": cached})
            self._remember_latest_turn(cached)
            return True

        await self._send({"type": "tool_call", "tool": intent.capability, "args": intent.args})
        result = await execute_skill(skill, intent.args, self._config)
        await self._send({"type": "tool_result", "tool": intent.capability, "result": result})
        if isinstance(result, dict) and result.get("error"):
            return False  # never answer with a wrong count on error — let the LLM try
        total = extract_list_count(result)
        if intent.capability == "warehouse":
            answer = f"{intent.entity_label[:1].upper()}{intent.entity_label[1:]}: {total}."
        else:
            answer = f"Всего {intent.entity_label}: {total}."
        cache_set(cache_key, answer)
        await self._send({"type": "text", "content": answer})
        self._remember_latest_turn(answer)
        return True

    async def _run(self) -> None:
        try:
            if not self._config.enabled:
                await self._send({
                    "type": "error",
                    "content": "Встроенный агент отключен в настройках.",
                })
                return

            # Deterministic fast-path: skip the LLM for high-confidence count questions.
            if await self._try_fast_intent():
                return

            await self._append_memory_context()
            await self._inject_rating_hint()
            await self._inject_learning_rules()

            consecutive_empty_responses = 0
            for iteration in range(self._config.max_steps):
                self._iteration = iteration

                # Context compression before each LLM call
                if self._compressor and self._compressor.should_compress(self.messages):
                    logger.info(
                        "compressing context",
                        session=self._session_id,
                        iteration=iteration,
                    )
                    await self._send({
                        "type": "status",
                        "content": "Сжимаю контекст сессии…",
                    })
                    self.messages = await self._compressor.compress(
                        self.messages,
                        self._call_for_compression,
                    )

                t_start = time.time()
                accumulated_text: list[str] = []

                async def on_token(token: str) -> None:
                    accumulated_text.append(token)

                # Thinking-model keepalive: send a status frame at most every 15 seconds
                # while the model emits reasoning_content (think phase).  This prevents
                # Traefik (default idle timeout ~180 s) from dropping the WebSocket.
                _last_thinking_ping: list[float] = [0.0]

                async def _on_thinking(chunk: str) -> None:
                    now = time.time()
                    if now - _last_thinking_ping[0] >= 15.0:
                        _last_thinking_ping[0] = now
                        try:
                            await self._send({
                                "type": "status",
                                "content": "Модель думает…",
                            })
                        except Exception:
                            pass

                model_override, provider_override, disable_thinking, thinking_level = _turn_model_overrides(
                    self._config,
                    self.messages,
                )
                # Tier-based override from the orchestrator (e.g. fast small model
                # for simple turns) — applies to worker turns, not builder turns.
                if self._turn_model_override and not _is_builder_turn(self.messages):
                    model_override = self._turn_model_override
                message = await _call_provider_streaming(
                    self.messages,
                    self._tools_for_turn(),
                    self._effective_system(),
                    self._config,
                    on_token,
                    model_override=model_override,
                    provider_override=provider_override,
                    disable_thinking_override=disable_thinking,
                    thinking_level_override=thinking_level,
                    on_thinking=_on_thinking,
                    max_tokens=self._response_budget,
                )
                self._accumulate_usage(message)
                duration_ms = int((time.time() - t_start) * 1000)
                tool_calls = message.get("tool_calls") or []
                full_text = "".join(accumulated_text)

                asyncio.create_task(self._log_action(
                    iteration=iteration,
                    action_type="llm_call",
                    content_text=full_text[:2000] if full_text else None,
                    model_name=model_override or _get_agent_model(self._config),
                    duration_ms=duration_ms,
                ))

                if not tool_calls:
                    if not full_text.strip():
                        consecutive_empty_responses += 1
                        logger.warning(
                            "agent_empty_llm_response",
                            session_id=self._session_id,
                            iteration=iteration,
                            consecutive_empty=consecutive_empty_responses,
                        )
                        if consecutive_empty_responses >= 2:
                            await self._send({
                                "type": "error",
                                "content": (
                                    "Модель вернула пустой ответ после вызова инструмента. "
                                    "Попробуйте повторить запрос или выбрать другую модель агента."
                                ),
                            })
                            break
                        # Nudge the next iteration so model finishes with either
                        # the next tool call or a final textual answer.
                        self.messages.append({
                            "role": "system",
                            "content": (
                                "Продолжи выполнение задачи: используй уже полученные "
                                "результаты инструментов и выдай следующий шаг "
                                "или финальный ответ пользователю."
                            ),
                        })
                        self._trim_history()
                        continue
                    consecutive_empty_responses = 0
                    delivered_text = await self._deliver_final_content(full_text)
                    self._record_assistant_reply(delivered_text)
                    # Fire-and-forget: index this turn into memory
                    self._remember_latest_turn(delivered_text)
                    break

                self.messages.append(message)

                # План — ДО выполнения, а не по факту. Раньше о замысле агента
                # можно было судить только по уже сделанным вызовам: «сделал не
                # то» обнаруживалось после того, как сделал. Показываем список
                # намерений словами; остановить ход человек может кнопкой
                # «Стоп», которая уже есть.
                await self._announce_plan(tool_calls, iteration)

                from app.ai.tool_parallelism import should_parallelize
                if should_parallelize(tool_calls):
                    results = await self._execute_tools_parallel(tool_calls, iteration)
                else:
                    results = await self._execute_tools_sequential(tool_calls, iteration)

                # Fast path: a single Workspace-publish tool already produced the
                # table AND a ready user message — deliver it directly instead of
                # burning another ~8 s LLM call just to say "таблица готова".
                publish_reply = self._terminal_publish_reply(results)
                if publish_reply:
                    await self._deliver_final_content(publish_reply)
                    self._remember_latest_turn(publish_reply)
                    break
            else:
                # max_steps exhausted while the model was still calling tools:
                # force a final textual answer from the gathered results instead
                # of ending the turn silently (which left the user with no reply).
                await self._force_final_answer()

        except Exception as e:
            logger.error(
                "agent_loop_error",
                error_type=type(e).__name__,
                error=str(e),
                model=self._config.worker_model if self._config else None,
                provider=_get_agent_provider(self._config) if self._config else None,
            )
            asyncio.create_task(self._log_action(
                iteration=self._iteration,
                action_type="error",
                error=str(e),
            ))
            try:
                await self._send({"type": "error", "content": f"Ошибка агента: {e}"})
            except Exception:
                pass
        finally:
            try:
                await self._send({"type": "done"})
            except Exception:
                pass

    def _record_assistant_reply(self, text: str) -> None:
        """Положить финальный ответ агента в историю хода.

        Ответ исполнителя в историю не попадал: в неё уходило только сообщение
        с вызовами инструментов. Из-за этого агент не помнил, что сам только
        что сказал человеку, — а именно на это человек и отвечает «да». Путь
        секретаря такую запись делал давно (record_external_turn), путь
        исполнителя — нет.
        """
        if not text or not str(text).strip():
            return
        if self.messages and self.messages[-1].get("role") == "assistant" \
                and self.messages[-1].get("content") == text:
            return
        self.messages.append({"role": "assistant", "content": text})
        self._trim_history()

    async def _deliver_final_content(self, full_text: str) -> str:
        text = (full_text or "").strip()
        if not text:
            return ""

        latest_user = next(
            (
                str(m.get("content", ""))
                for m in reversed(self.messages)
                if m.get("role") == "user"
            ),
            "",
        )
        parsed_table = _parse_markdown_table(text)
        if parsed_table:
            title, columns, rows = parsed_table
            await self._publish_canvas(
                {
                    "type": "table",
                    "title": title,
                    "columns": columns,
                    "rows": rows,
                },
                canvas_id=_agent_canvas_id("llm-table"),
                append=False,
            )
            summary = f"Открыл таблицу на Рабочем столе: {len(rows)} строк."
            await self._send({"type": "text", "content": summary})
            return summary

        # Reliable fallback: when the orchestrator routed this turn to the desktop
        # (by intent), a substantial non-table result is still published there —
        # no dependency on keyword markers in the user's phrasing. The legacy
        # keyword gate remains for turns the orchestrator didn't classify.
        publish_to_desktop = (
            self._workspace_expected
            or _is_workspace_output_request(latest_user)
        )
        if publish_to_desktop and len(text) > 200:
            await self._publish_canvas(
                {"type": "markdown", "title": "Результат", "content": text},
                canvas_id=_agent_canvas_id("llm-result"),
                append=False,
            )
            summary = "Открыл результат на Рабочем столе."
            await self._send({"type": "text", "content": summary})
            return summary

        await self._send({"type": "text", "content": text})
        return text

    async def _force_final_answer(self) -> None:
        """Produce a final user-facing reply when the step budget is exhausted.

        Makes one tool-less LLM call so the model summarises the results it has
        already gathered (instead of the turn ending silently). Falls back to a
        plain message if even that yields nothing — the turn must never go quiet.
        """
        self.messages.append({
            "role": "system",
            "content": (
                "Достигнут лимит шагов. Сформулируй краткий финальный ответ "
                "пользователю на основе уже полученных результатов инструментов. "
                "НЕ вызывай инструменты — только текст."
            ),
        })
        acc: list[str] = []

        async def _on_token(token: str) -> None:
            acc.append(token)

        msg: dict = {}
        try:
            msg = await _call_provider_streaming(
                self.messages,
                [],  # no tools → forces a textual answer
                self._effective_system(),
                self._config,
                _on_token,
                disable_thinking_override=True,
                max_tokens=self._response_budget,
            )
            self._accumulate_usage(msg)
        except Exception as e:  # noqa: BLE001
            logger.warning("force_final_answer_failed", error=str(e), session_id=self._session_id)

        text = "".join(acc).strip()
        if not text and isinstance(msg, dict):
            text = str(msg.get("content") or "").strip()
        if text:
            await self._deliver_final_content(text)
            self._record_assistant_reply(text)
            self._remember_latest_turn(text)
        else:
            await self._send({
                "type": "text",
                "content": (
                    "Не удалось полностью завершить задачу за отведённое число шагов. "
                    "Уточните запрос или разбейте его на части."
                ),
            })

    async def _execute_single_tool(
        self, tc: dict, iteration: int
    ) -> tuple[str, dict, str]:
        """Execute one tool call and return (fn_name, result, tool_call_id).

        Does NOT append to messages. The tool_call_id is threaded back so the
        result message can be linked to its originating call (OpenAI spec
        requires it; Anthropic matches tool_result.tool_use_id by id, not order).
        """
        fn = tc.get("function", {})
        fn_name = fn.get("name", "")
        tc_id = tc.get("id") or ""
        raw_args = fn.get("arguments", {})
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args or "{}")
            except (json.JSONDecodeError, TypeError):
                # Surface a structured error so the model can self-correct by
                # re-issuing the call with valid JSON, instead of silently
                # executing with empty args or crashing the whole turn.
                result = {
                    "error_code": "bad_arguments",
                    "error": "Аргументы инструмента не являются валидным JSON.",
                    "hint": "Повтори вызов с корректным JSON-объектом аргументов.",
                }
                await self._send({"type": "tool_result", "tool": fn_name, "result": result})
                return fn_name, result, tc_id

        asyncio.create_task(self._log_action(
            iteration=iteration,
            action_type="tool_call",
            tool_name=fn_name,
            tool_args=args,
        ))
        await self._send({"type": "tool_call", "tool": fn_name, "args": args})

        skill = self._skill_map.get(fn_name)
        original_name = skill["name"] if skill else fn_name.replace("__", ".")

        # Re-read approval_gates from latest config at every tool call (not cached from session start).
        from app.ai.agent_config import get_builtin_agent_config as _get_latest_config
        from app.ai.policy_engine import check_tool_execution
        current_gates = set((_get_latest_config()).approval_gates)

        # Capabilities mode: check gate_actions declared in capabilities.yml.
        # "*" gates every action (used by "mcp", Б17) — mirrors the wildcard
        # capability_router._enforce_capability_policy applies at the actual
        # HTTP boundary. That boundary is the real backstop either way (a miss
        # here just means the LLM sees a raw 423 instead of a proper
        # approval-request UX), but checking it here too keeps the two in
        # sync instead of silently relying on the second one to catch it.
        cap_gate_actions = set()
        if skill:
            cap_gate_actions = set(skill.get("gate_actions") or [])
        action_arg = args.get("action", "")
        if action_arg and (action_arg in cap_gate_actions or "*" in cap_gate_actions):
            current_gates.add(original_name)

        policy = check_tool_execution(
            skill_name=original_name,
            args=args,
            config=self._config,
            approval_gates=current_gates,
        )
        asyncio.create_task(self._log_action(
            iteration=iteration,
            action_type="policy_check",
            tool_name=original_name,
            tool_result={
                "allowed": policy.allowed,
                "risk_level": policy.risk_level,
                "reason": policy.reason,
                "required_approval": policy.required_approval,
            },
        ))
        if not policy.allowed:
            result = {
                "status": "blocked",
                "message": policy.reason,
                "risk_level": policy.risk_level,
                "required_approval": policy.required_approval,
            }
            await self._send({"type": "tool_result", "tool": fn_name, "result": result})
            return fn_name, result, tc_id

        approval_granted = False
        _authorized_by = None
        if original_name in current_gates:
            if self._explicit_send_authorized(original_name, args):
                _authorized_by = ("user:explicit_instruction",
                                  "Отправка по вашему прямому указанию.")
            elif self._confirms_pending_send(original_name, args):
                # Человек только что ответил «да» на показанный черновик.
                _authorized_by = ("user:confirmed_proposal",
                                  "Отправляю — вы подтвердили это письмо.")
        if _authorized_by:
            # Согласие человека заменяет запрос подтверждения. Полностью
            # аудируется: в журнале видно, что именно послужило разрешением.
            asyncio.create_task(self._log_action(
                iteration=iteration,
                action_type="approval_decision",
                tool_name=original_name,
                tool_args=args,
                tool_result={"approved": True, "actor": _authorized_by[0]},
            ))
            await self._send({
                "type": "approval_auto",
                "tool": original_name,
                "message": _authorized_by[1],
            })
            self._granted_approvals.add(self._approval_key(original_name, args))
            approval_granted = True
        elif original_name in current_gates and self._approval_key(
            original_name, args
        ) in self._granted_approvals:
            # Это же действие человек уже одобрил в этом ходе — повторный
            # запрос был бы вопросом о том, на что уже ответили.
            asyncio.create_task(self._log_action(
                iteration=iteration,
                action_type="approval_decision",
                tool_name=original_name,
                tool_result={"approved": True, "actor": "user:already_approved_this_turn"},
            ))
            approval_granted = True
        elif original_name in current_gates:
            asyncio.create_task(self._log_action(
                iteration=iteration,
                action_type="approval_request",
                tool_name=original_name,
                tool_args=args,
            ))
            approved = await self._request_approval(original_name, args)
            asyncio.create_task(self._log_action(
                iteration=iteration,
                action_type="approval_decision",
                tool_name=original_name,
                tool_result={"approved": approved},
            ))
            if not approved:
                result: dict = {"status": "rejected", "message": "Отклонено пользователем"}
                await self._send({"type": "tool_result", "tool": fn_name, "result": result})
                return fn_name, result, tc_id
            self._granted_approvals.add(self._approval_key(original_name, args))
            approval_granted = True

        # Человек поправил содержимое в карточке — вызов уходит с новыми
        # аргументами (для письма это свежий expected_digest, иначе отправка
        # упрётся в 409 «черновик изменился»).
        if approval_granted and self._pending_args_override:
            args = {**args, **self._pending_args_override}
            self._pending_args_override = None

        if skill:
            result = await execute_skill(
                skill,
                args,
                self._config,
                approval_granted=approval_granted,
            )
        else:
            available = sorted(self._skill_map.keys())[:30]
            result = {
                "error_code": "unknown_skill",
                "error": f"Unknown skill: {fn_name}",
                "available_skills": available,
                "hint": "Проверь имя скилла — используй двойное подчёркивание вместо точки (например invoice__list).",
            }

        asyncio.create_task(self._log_action(
            iteration=iteration,
            action_type="tool_result",
            tool_name=fn_name,
            tool_result=result if len(str(result)) < 2000 else {"truncated": True},
        ))
        await self._send({"type": "tool_result", "tool": fn_name, "result": result})
        return fn_name, result, tc_id

    async def _tool_result_to_history(self, result: dict, tool_call_id: str = "") -> None:
        """Serialise a tool result for conversation history.

        Results exceeding VAULT_THRESHOLD are stored in Redis; the history
        receives a compact envelope (preview + vault_ref) instead of the full
        payload, keeping the context window thin as the dataset grows.
        """
        from app.ai.turn_vault import make_vault_envelope, should_vault, vault_store
        content_json = json.dumps(result, ensure_ascii=False)
        if should_vault(content_json):
            try:
                ref = await vault_store(self._session_id, result)
                envelope = make_vault_envelope(result, ref)
                content_json = json.dumps(envelope, ensure_ascii=False)
            except Exception:
                # Vault unavailable: fall back to trimmed result
                pass
        msg: dict = {
            "role": "tool",
            "content": _trim_tool_result(content_json),
        }
        if tool_call_id:
            msg["tool_call_id"] = tool_call_id
        self.messages.append(msg)

    async def _announce_plan(self, tool_calls: list[dict], iteration: int) -> None:
        """Сказать словами, что агент собирается сделать в этом шаге.

        Только когда шагов больше одного или действие необратимое: для
        одиночного «посмотрю список» объявление было бы шумом.
        """
        from app.ai.approval_preview import describe_call, is_irreversible

        steps: list[dict] = []
        for tc in tool_calls:
            fn = (tc.get("function") or {})
            raw_name = str(fn.get("name") or "")
            skill = self._skill_map.get(raw_name)
            name = skill["name"] if skill else raw_name.replace("__", ".")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            steps.append({
                "tool": name,
                "text": describe_call(name, args),
                "irreversible": is_irreversible(name, args),
            })

        if len(steps) < 2 and not any(s["irreversible"] for s in steps):
            return
        await self._send({"type": "plan", "iteration": iteration, "steps": steps})

    async def _execute_tools_sequential(
        self, tool_calls: list[dict], iteration: int
    ) -> list[tuple[str, dict]]:
        results: list[tuple[str, dict]] = []
        for tc in tool_calls:
            fn_name, result, tc_id = await self._execute_single_tool(tc, iteration)
            results.append((fn_name, result))
            await self._tool_result_to_history(result, tc_id)
            self._trim_history()
        return results

    async def _execute_tools_parallel(
        self, tool_calls: list[dict], iteration: int
    ) -> list[tuple[str, dict]]:
        # Observability marker — lets the orchestrator log parallel_used per turn.
        await self._send({"type": "tools.parallel", "count": len(tool_calls)})
        results = await asyncio.gather(
            *[self._execute_single_tool(tc, iteration) for tc in tool_calls],
            return_exceptions=False,
        )
        for _fn_name, result, tc_id in results:
            await self._tool_result_to_history(result, tc_id)
        self._trim_history()
        return [(fn_name, result) for fn_name, result, _tc_id in results]

    @staticmethod
    def _terminal_publish_reply(results: list[tuple[str, dict]]) -> str | None:
        """If a single tool published a Workspace block and returned a ready
        user-facing message, that message IS the answer — no second LLM round
        trip needed (it just paraphrases "table published"). Returns the message
        for that fast-path, else None.
        """
        if len(results) != 1:
            return None
        _fn, res = results[0]
        if (
            isinstance(res, dict)
            and res.get("status") == "published"
            and res.get("canvas_id")
            and res.get("message")
        ):
            return str(res["message"])
        return None

    _EXPLICIT_SEND_RE = re.compile(
        r"(отправ|пошл[иёе]|разошл|send|отош)[а-яё]*[\s\S]{0,60}"
        r"(письм|сообщени|email|e-mail|мейл|запрос|кп|коммерческ)",
        re.IGNORECASE,
    )

    # Короткое согласие в ответ на показанный черновик. Намеренно узкий
    # список: это ответ «да» на конкретный вопрос, а не разговорное «ладно»
    # посреди обсуждения.
    _CONFIRMATION_RE = re.compile(
        r"^\W*(да|ага|угу|ок|окей|хорошо|давай(те)?|подтвержда[юе][а-яё]*|"
        r"отправ(ляй|ь|ляйте|ьте)|поехали|верно|согласен|согласна|"
        r"yes|ok|okay|sure|confirm(ed)?|send( it)?)\W*$",
        re.IGNORECASE,
    )
    # Отрицание внутри короткого ответа отменяет согласие целиком: «да, но не
    # отправляй» — это отказ, а не подтверждение.
    _NEGATION_RE = re.compile(
        r"(\bне\b|\bнет\b|погод|подожд|стоп|отмен|don'?t|no\b|wait|cancel|stop)",
        re.IGNORECASE,
    )
    # Признак того, что предыдущий ход агента ЗАКОНЧИЛСЯ предложением
    # отправить письмо: без этого «да» относилось бы неизвестно к чему.
    _SEND_PROPOSAL_RE = re.compile(
        r"(подтвержда[ею]те?\s+отправк|подтвердит[ье]\s+отправк|отправ[а-яё]*\s*\?|"
        r"отправля[ею]м\?|(да|нет)\s*[/)]|черновик письма|вот что будет отправлено)",
        re.IGNORECASE,
    )

    def _confirms_pending_send(self, skill_name: str, args: dict) -> bool:
        """True, когда человек только что ответил «да» на показанный черновик.

        Живой случай: агент показал письмо и спросил «Подтверждаю отправку?»,
        человек ответил «да» — и получил ещё шесть запросов разрешения на то
        же самое письмо. Ответ на вопрос и есть разрешение; спрашивать снова —
        значит спрашивать о том, на что уже ответили.

        Условия намеренно жёсткие, потому что это гейт внешнего действия:
        предыдущий ход агента должен заканчиваться предложением отправить,
        ответ человека должен быть коротким согласием без отрицания, а если
        вызов несёт получателя и тему — они обязаны совпасть с тем, что было
        показано. Иначе подтверждали бы одно письмо, а уходило бы другое.
        """
        if skill_name not in ("email", "email.send"):
            return False
        if args.get("action") not in (None, "send"):
            return False
        # «Да» подтверждает показанное письмо, но не снимает блокирующий риск:
        # признать риск приемлемым человек должен явно, глядя на его причину.
        if args.get("acknowledged_risks"):
            return False

        last_user = next(
            (str(m.get("content") or "") for m in reversed(self.messages)
             if m.get("role") == "user"),
            "",
        )
        if not last_user.strip() or len(last_user) > 64:
            return False
        if self._NEGATION_RE.search(last_user):
            return False
        if not self._CONFIRMATION_RE.match(last_user.strip()):
            return False

        # Предложение агента ищем ДО этого сообщения человека: подтверждать
        # можно только уже показанное.
        proposal = ""
        seen_user = False
        for m in reversed(self.messages):
            role = m.get("role")
            if role == "user" and not seen_user:
                seen_user = True
                continue
            if seen_user and role == "assistant" and m.get("content"):
                proposal = str(m.get("content"))
                break
        if not proposal or not self._SEND_PROPOSAL_RE.search(proposal):
            return False

        # Привязка к содержимому: то, что видно в аргументах, должно быть в
        # показанном тексте. Для вызова с одним draft_id подмену содержимого
        # ловит content_digest на стороне API.
        body = args.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                body = {}
        payload = body if isinstance(body, dict) else {}
        recipients = payload.get("to_addresses") or args.get("to_addresses") or []
        low_proposal = proposal.lower()
        for addr in recipients if isinstance(recipients, list) else [recipients]:
            if str(addr).lower() not in low_proposal:
                return False
        return True

    @staticmethod
    def _approval_key(skill_name: str, args: dict) -> str:
        """Что именно человек одобрил — чтобы не спрашивать об этом дважды.

        Ключ описывает ПРЕДМЕТ действия, а не форму вызова. Модель, не поверив
        успешному ответу, переспрашивала отправку одного и того же черновика
        шестью разными наборами аргументов — человек шесть раз нажимал
        «Утверждено» на одно и то же письмо.

        Безопасность не слабеет: изменится черновик или получатель — изменится
        и ключ, и разрешение спросят заново. А подмену содержимого уже
        одобренного черновика ловит content_digest на стороне API.
        """
        # Подтверждают КОНКРЕТНЫЙ текст: digest письма входит в ключ, поэтому
        # одобрение, данное на одно содержимое, не переносится на другое.
        # Раньше комментарий отсылал к content_digest «на стороне API», а тот
        # был необязательным параметром — круг замыкался на предположении,
        # которое никто не проверял.
        digest = args.get("expected_digest") or ""
        ident = args.get("draft_id") or args.get("id")
        if ident and digest:
            ident = f"{ident}@{digest}"
        if not ident:
            body = args.get("body")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except Exception:
                    body = {}
            payload = body if isinstance(body, dict) else {}
            ident = json.dumps(
                {
                    "to": payload.get("to_addresses") or args.get("to_addresses"),
                    "subject": payload.get("subject") or args.get("subject"),
                },
                ensure_ascii=False, sort_keys=True,
            )
        return f"{skill_name}:{args.get('action') or ''}:{ident}"

    def _explicit_send_authorized(self, skill_name: str, args: dict) -> bool:
        """True, когда человек в этом ходе прямо велел ОТПРАВИТЬ письмо,
        которое он уже видел.

        Ключевое слово — «видел». Раньше здесь хватало приказа: фраза вроде
        «отправь поставщику письмо с просьбой прислать счёт» выдавала
        разрешение, и наружу уходил текст, который человеку никто не показывал.
        Это ровно то, что запрещает draft-first: подтверждают письмо, а не
        намерение написать письмо. Теперь требуется и приказ, и показанное
        содержимое (``_draft_shown_in_turn``); если письма ещё не показывали,
        вызов идёт обычным путём — человек увидит превью и решит сам.

        Второе исправление — отрицание. Проверялась одна подстрока «не отправл»,
        поэтому «не нужно отправлять письмо поставщику, просто подготовь»
        считалось приказом отправить. Теперь работает общий ``_NEGATION_RE``.
        """
        if skill_name not in ("email", "email.send"):
            return False
        if args.get("action") not in (None, "send"):
            return False
        # Признание блокирующего риска — решение человека, а не модели: такой
        # вызов всегда идёт через явный запрос подтверждения.
        if args.get("acknowledged_risks"):
            return False
        last_user = next(
            (str(m.get("content") or "") for m in reversed(self.messages)
             if m.get("role") == "user"),
            "",
        )
        if not last_user or not self._EXPLICIT_SEND_RE.search(last_user):
            return False
        low = last_user.lower()
        if "черновик" in low or "draft" in low:
            return False
        if self._NEGATION_RE.search(last_user):
            return False
        has_recipient = (
            "@" in last_user
            or any(w in low for w in ("поставщик", "клиент", "контрагент", "заказчик", "адрес"))
        )
        return has_recipient and self._draft_shown_in_turn(args)

    def _draft_snapshot(self, args: dict) -> dict:
        """Получатели и тема письма, которое отправляет этот вызов.

        Либо прямо из аргументов, либо — когда в вызове только ``draft_id`` —
        из результата инструмента, которым этот черновик был создан/прочитан в
        текущей сессии.
        """
        body = args.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:  # noqa: BLE001
                body = {}
        payload = body if isinstance(body, dict) else {}
        recipients = payload.get("to_addresses") or args.get("to_addresses")
        subject = payload.get("subject") or args.get("subject")
        if recipients:
            return {
                "to": [str(a) for a in (recipients if isinstance(recipients, list)
                                        else [recipients])],
                "subject": str(subject or ""),
            }

        draft_id = str(args.get("draft_id") or args.get("id") or "")
        if not draft_id:
            return {"to": [], "subject": ""}
        for msg in reversed(self.messages):
            if msg.get("role") != "tool":
                continue
            content = str(msg.get("content") or "")
            if draft_id not in content:
                continue
            try:
                data = json.loads(content)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            to = data.get("to_addresses")
            if to:
                return {
                    "to": [str(a) for a in (to if isinstance(to, list) else [to])],
                    "subject": str(data.get("subject") or ""),
                }
        return {"to": [], "subject": ""}

    def _draft_shown_in_turn(self, args: dict) -> bool:
        """Показывал ли агент это письмо человеку в текущем ходе.

        «Показывал» — значит в тексте, который агент вывел после последнего
        сообщения человека, есть и получатель, и тема письма. Пустой снимок
        (ни получателей, ни темы) показанным не считается: тогда сверять
        нечего, и правильный ответ — спросить.
        """
        snapshot = self._draft_snapshot(args)
        recipients = [a for a in snapshot["to"] if a]
        if not recipients:
            return False

        shown: list[str] = []
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                break
            if msg.get("role") == "assistant" and msg.get("content"):
                shown.append(str(msg["content"]))
        if not shown:
            return False
        haystack = "\n".join(shown).lower()

        if not all(str(a).lower() in haystack for a in recipients):
            return False
        subject = snapshot["subject"].strip().lower()
        return not subject or subject in haystack

    async def _request_approval(self, skill_name: str, args: dict) -> bool:
        from app.ai.approval_preview import build_preview

        # Карточка вместо json.dumps(args): человек, утверждающий отправку
        # письма, должен видеть письмо, а не draft_id и дайджест. Сырые
        # аргументы остаются внутри карточки для тех, кому они нужны.
        card = await build_preview(skill_name, args)
        preview = json.dumps(args, ensure_ascii=False, indent=2)

        db_id: str | None = None
        try:
            db_id = await _create_db_approval(skill_name, args, card)
        except Exception as exc:
            log_degraded("agent_loop.approval_create", exc, skill=skill_name)
        if _approval_action_type_for(skill_name, args) and not db_id:
            await self._send({
                "type": "approval_error",
                "tool": skill_name,
                "message": (
                    "Durable approval record was not created; gated action "
                    "is blocked fail-closed."
                ),
            })
            return False

        approved = False
        max_attempts = 2
        approval_id = str(uuid.uuid4())
        self._pending_approval_id = approval_id
        # Чтобы решение, принятое на странице согласований, нашло этот ход.
        self._pending_db_id = db_id
        for attempt in range(1, max_attempts + 1):
            self._approval_future = asyncio.get_event_loop().create_future()
            await self._send({
                "type": "approval_request",
                "tool": skill_name,
                "args": args,
                "preview": preview,
                "card": card.as_dict(),
                # Необратимое действие никогда не проходит по «подтвердить
                # всё»: письмо, платёж и удаление решаются поимённо.
                "irreversible": card.irreversible,
                "approval_id": approval_id,
                "db_id": db_id,
                "attempt": attempt,
                "max_attempts": max_attempts,
            })
            try:
                approved = await asyncio.wait_for(
                    self._approval_future,
                    timeout=float(self._config.approval_timeout_seconds),
                )
                break
            except TimeoutError:
                self._approval_future = None
                if attempt < max_attempts:
                    await self._send({
                        "type": "approval_timeout",
                        "tool": skill_name,
                        "attempt": attempt,
                        "message": (
                            f"Запрос подтверждения для {skill_name!r} не получил ответа. "
                            f"Повторный запрос ({attempt + 1}/{max_attempts})…"
                        ),
                    })
                else:
                    await self._send({
                        "type": "approval_timeout",
                        "tool": skill_name,
                        "attempt": attempt,
                        "message": (
                            f"Запрос подтверждения для {skill_name!r} истёк {max_attempts} раза. "
                            "Действие отклонено автоматически."
                        ),
                    })
        self._approval_future = None
        self._pending_approval_id = None
        self._pending_db_id = None

        if db_id:
            try:
                await _decide_db_approval(db_id, approved)
            except Exception as exc:
                log_degraded("agent_loop.approval_decide", exc)

        return approved

    def _trim_history(self) -> None:
        keep = self._config.max_history_messages
        if len(self.messages) > keep:
            self.messages = self.messages[-keep:]
        # Eagerly prune old tool results: keep last 6 verbatim, replace older
        # ones with a stub. This is free (no LLM call) and prevents tool result
        # payloads from accumulating across turns.
        from app.ai.context_compressor import _prune_old_tool_results
        self.messages = _prune_old_tool_results(self.messages, keep_last=6)

    async def _append_memory_context(self) -> None:
        if not self._config.memory_enabled:
            return
        latest_user = next(
            (
                message.get("content", "")
                for message in reversed(self.messages)
                if message.get("role") == "user"
            ),
            "",
        )
        if not latest_user:
            return
        # Gate: skip RAG for pure workspace/flow queries answered from SQL.
        # Saves a vector search + reranker round-trip and keeps context clean.
        from app.ai import route_table
        if not route_table.needs_document_retrieval(latest_user):
            return
        try:
            context = await asyncio.wait_for(
                self._memory_mgr.prefetch(latest_user, session_id=self._session_id),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            context = ""
        if not context:
            return
        self.messages.append({
            "role": "system",
            "content": (
                "Контекст из долговременной памяти проекта. Используй его как "
                "справочный материал и проверяй через инструменты при критичных "
                f"действиях.\n{context}"
            ),
        })
        asyncio.create_task(self._log_action(
            iteration=self._iteration,
            action_type="memory_context",
            content_text=context[:2000],
        ))
        self._trim_history()


def extract_list_count(payload: Any) -> int:
    if isinstance(payload, dict):
        for key in ("total", "count", "items_total", "results_count"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
        for list_key in ("items", "results", "data", "rows"):
            value = payload.get(list_key)
            if isinstance(value, list):
                return len(value)
        return 0
    if isinstance(payload, list):
        return len(payload)
    return 0


def _parse_markdown_table(
    text: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]] | None:
    lines = [line.strip() for line in text.splitlines()]
    start = -1
    for idx in range(len(lines) - 1):
        if "|" not in lines[idx] or "|" not in lines[idx + 1]:
            continue
        separator = lines[idx + 1].replace("|", "").replace(":", "").replace("-", "").strip()
        if not separator:
            start = idx
            break
    if start < 0:
        return None

    def split_row(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip("|").split("|")]

    headers = split_row(lines[start])
    if len(headers) < 2:
        return None
    rows: list[dict[str, Any]] = []
    for line in lines[start + 2:]:
        if "|" not in line:
            break
        cells = split_row(line)
        if len(cells) < 2:
            break
        row: dict[str, Any] = {}
        for col_idx, header in enumerate(headers):
            key = f"col_{col_idx + 1}"
            row[key] = cells[col_idx] if col_idx < len(cells) else ""
        rows.append(row)
    if not rows:
        return None
    columns = [
        {"key": f"col_{idx + 1}", "header": header or f"Колонка {idx + 1}", "type": "text"}
        for idx, header in enumerate(headers)
    ]
    title = "Таблица"
    for line in reversed(lines[:start]):
        clean = line.strip("#* ")
        if clean:
            title = clean[:120]
            break
    return title, columns, rows


# ── DB approval helpers ───────────────────────────────────────────────────────
# Maps skill names to ApprovalActionType enum values supported by the DB.
_APPROVAL_ACTION_TYPE_MAP: dict[str, str] = {
    "invoice.approve": "invoice.approve",
    "invoice.reject": "invoice.reject",
    "invoice.bulk_delete": "invoice.bulk_delete",
    "email.send": "email.send",
    "anomaly.resolve": "anomaly.resolve",
    "norm.activate_rule": "norm.activate_rule",
    "compare.decide": "compare.decide",
    "warehouse.confirm_receipt": "warehouse.confirm_receipt",
    "payment.mark_paid": "payment.mark_paid",
    "procurement.send_rfq": "procurement.send_rfq",
    "bom.approve": "bom.approve",
    "bom.create_purchase_request": "bom.create_purchase_request",
    "tech.process_plan_approve": "tech.process_plan_approve",
    "tech.norm_estimate_approve": "tech.norm_estimate_approve",
    "tech.learning_rule_activate": "tech.learning_rule_activate",
}

_CAPABILITY_APPROVAL_ACTION_TYPE_MAP: dict[tuple[str, str], str] = {
    ("invoices", "approve"): "invoice.approve",
    ("invoices", "reject"): "invoice.reject",
    ("invoices", "bulk_delete"): "invoice.bulk_delete",
    ("email", "send"): "email.send",
    ("anomalies", "resolve"): "anomaly.resolve",
    ("normalization", "activate_rule"): "norm.activate_rule",
    ("analytics", "compare_decide"): "compare.decide",
    ("analytics", "table_apply_diff"): "table.apply_diff",
    ("warehouse", "confirm_receipt"): "warehouse.confirm_receipt",
    ("payments", "mark_paid"): "payment.mark_paid",
    ("procurement", "send_rfq"): "procurement.send_rfq",
    ("tech", "bom_approve"): "bom.approve",
    ("tech", "bom_purchase_request"): "bom.create_purchase_request",
    ("tech", "process_plan_approve"): "tech.process_plan_approve",
    ("tech", "norm_estimate_approve"): "tech.norm_estimate_approve",
    ("tech", "learning_rule_activate"): "tech.learning_rule_activate",
}


def _approval_action_type_for(skill_name: str, args: dict) -> str | None:
    action = str(args.get("action") or "")
    return (
        _APPROVAL_ACTION_TYPE_MAP.get(skill_name)
        or _CAPABILITY_APPROVAL_ACTION_TYPE_MAP.get((skill_name, action))
    )


async def _create_db_approval(skill_name: str, args: dict, card=None) -> str | None:
    """Create an Approval record in DB and return its ID.

    ``card`` — человекочитаемое описание (app.ai.approval_preview). Оно
    сохраняется в context, чтобы список ожидающих решений (/inbox,
    /approvals) показывал «Отправить письмо · Ромекс», а не имя действия и
    набор идентификаторов.
    """
    action_type = _approval_action_type_for(skill_name, args)
    if not action_type:
        return None  # DB enum doesn't support this gate yet (Этап 10)

    entity_id_str = (
        args.get("invoice_id")
        or args.get("document_id")
        or args.get("anomaly_id")
        or args.get("receipt_id")
        or args.get("schedule_id")
        or args.get("request_id")
        or args.get("bom_id")
        or args.get("plan_id")
        or args.get("estimate_id")
        or args.get("rule_id")
        or args.get("entity_id")
        or str(uuid.uuid4())
    )
    try:
        entity_id = str(uuid.UUID(str(entity_id_str)))
    except ValueError:
        entity_id = str(uuid.uuid4())

    entity_type = action_type.split(".", 1)[0]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{get_builtin_agent_config().backend_url.rstrip('/')}/api/approvals",
            json={
                "action_type": action_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "requested_by": "sveta",
                "context": {
                    **args,
                    **(
                        {
                            "title": card.title,
                            "subtitle": card.subtitle,
                            "irreversible": card.irreversible,
                            "preview_card": card.as_dict(),
                        }
                        if card is not None
                        else {}
                    ),
                },
            },
            headers=internal_headers(),
        )
        if resp.status_code == 201:
            return resp.json().get("id")
    return None


async def _decide_db_approval(approval_id: str, approved: bool) -> None:
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            f"{get_builtin_agent_config().backend_url.rstrip('/')}/api/approvals/"
            f"{approval_id}/decide",
            json={
                "status": "approved" if approved else "rejected",
                "decided_by": "user",
            },
            headers=internal_headers(),
        )
