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
from app.config import settings as _settings

logger = structlog.get_logger()


def _internal_headers() -> dict:
    """Headers for agent → backend service calls (auth + internal marker)."""
    h: dict = {"X-Internal-Agent": "1"}
    if _settings.agent_service_key:
        h["X-API-Key"] = _settings.agent_service_key
    return h


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
- Память автоматическая: используй memory.search с retrieval_mode=auto_hybrid.
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
        from app.api.ai_settings import get_ai_config

        override = get_ai_config().get("model_agent")
        if override and str(override).strip():
            return str(override).strip()
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
) -> tuple[str | None, str | None, bool | None]:
    if _is_builder_turn(messages):
        return (
            config.builder_model or config.worker_model,
            config.builder_provider or config.worker_provider,
            config.builder_disable_thinking,
        )
    return (
        config.worker_model,
        config.worker_provider,
        config.worker_disable_thinking,
    )


def _thinking_disabled(
    config: BuiltinAgentConfig,
    override: bool | None = None,
) -> bool:
    return override if override is not None else config.disable_thinking


# ── Registry loading ──────────────────────────────────────────────────────────

def _sanitize_name(name: str) -> str:
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

        fn_name = _sanitize_name(name)
        description = (cap.get("description") or name).strip()

        tools.append({
            "type": "function",
            "function": {
                "name": fn_name,
                "description": description[:400],
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
                fn_name = _sanitize_name(gen_name)
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


def _load_registry(
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

        fn_name = _sanitize_name(skill["name"])
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
    return _load_registry(expose_filter)


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

async def _execute_skill(
    skill: dict,
    args: dict,
    config: BuiltinAgentConfig,
) -> dict:
    method = skill["method"].upper()
    path = skill["path"]
    base_url = config.backend_url.rstrip("/")
    timeout = config.backend_timeout_seconds

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
            _hdrs = _internal_headers()
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

async def _call_ollama_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    model_override: str | None = None,
    disable_thinking: bool | None = None,
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
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "tools": tools,
        "stream": True,
        "options": options,
    }
    if _thinking_disabled(config, disable_thinking):
        payload["think"] = False

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
    try:
        base_url, env_key, extra = {**mapping, **local_mapping}[provider]
    except KeyError as exc:
        raise ValueError(f"Unsupported openai-compatible provider: {provider}") from exc
    return base_url, os.environ.get(env_key, ""), extra


async def _call_openai_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    provider: str,
    model_override: str | None = None,
    disable_thinking: bool | None = None,
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
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "stream": True,
        "temperature": config.temperature,
    }
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    if tools:
        payload["tools"] = tools
    if _thinking_disabled(config, disable_thinking):
        if provider == "llamacpp":
            # llama.cpp uses chat_template_kwargs to control thinking in Qwen3/thinking models.
            # The generic OpenAI "reasoning" field is NOT supported by llama.cpp.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        else:
            # OpenAI-compatible providers (OpenRouter, DeepSeek, etc.) use this
            # to suppress reasoning traces on thinking models.
            payload["reasoning"] = {"enabled": False}

    full_content = ""
    scrubber = StreamingContextScrubber()
    # Accumulate streamed tool calls: index → {id, name, arguments}
    tool_acc: dict[int, dict[str, str]] = {}

    async with httpx.AsyncClient(timeout=float(config.llm_timeout_seconds)) as client:
        async with client.stream(
            "POST", f"{base_url}/chat/completions", headers=headers, json=payload
        ) as resp:
            resp.raise_for_status()
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
            tc_id = pending_ids.pop(0) if pending_ids else f"toolu_unknown_{len(pending_results)}"
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
    "vllm",
    "lmstudio",
    "openai_compatible",
    "llamacpp",
    "openrouter",
    "deepseek",
    "openai",
    "gemini",
    "mistral",
    "groq",
    "together",
    "fireworks",
    "xai",
    "cohere",
    "perplexity",
    "minimax",
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

        self._config = get_builtin_agent_config()
        self._rebuild_runtime_components(self._config)
        self._mcp_initialised = False
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
                    headers=_internal_headers(),
                )
        except Exception as exc:
            log_degraded("agent_loop.action_log", exc)

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

        from app.ai.context_compressor import ContextCompressor
        self._compressor = ContextCompressor(
            model=_get_agent_model(self._config),
            threshold_percent=self._config.context_compression_threshold,
            compression_model=self._config.compression_model,
        ) if self._config.context_compression_enabled else None

        from app.ai.memory_manager import MemoryManager
        self._memory_mgr = MemoryManager(
            base_url=self._config.backend_url,
            headers=_internal_headers(),
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

    def set_response_budget(self, max_tokens: int) -> None:
        """Set the per-turn max response tokens (clamped to a sane range)."""
        self._response_budget = max(256, min(int(max_tokens), 16384))

    def set_model_override(self, model: str | None) -> None:
        """Set a per-turn worker model (tier-based fast/strong routing).

        Replaced each turn. None → fall back to the configured worker/model.
        Does not affect builder turns (capability generation keeps builder_model).
        """
        self._turn_model_override = (model or "").strip() or None

    # Capabilities every role can always use, regardless of its allowlist.
    _CORE_CAPABILITIES = frozenset({"workspace", "memory", "search"})

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
        visible = set(allowed) | self._CORE_CAPABILITIES

        return _apply_exclusions([
            tool
            for tool in self._tools
            if _tool_name(tool) not in capability_names or _tool_name(tool) in visible
        ])

    def _effective_system(self) -> str:
        """Base system prompt plus the per-turn role context (if any)."""
        if self._role_context:
            return f"{self._system}\n\n## Роль в этой задаче\n{self._role_context}"
        return self._system

    async def on_user_message(self, content: str) -> None:
        self._refresh_runtime_config()
        await self._init_mcp()
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
                    headers=_internal_headers(),
                )
        except Exception as exc:
            log_degraded("agent_loop.canvas_publish", exc)
        await self._send({
            "type": "canvas",
            "canvas_id": canvas_id,
            "block": block,
            "append": append,
        })

    async def on_approval(self, approved: bool) -> None:
        if self._approval_future and not self._approval_future.done():
            self._approval_future.set_result(approved)

    async def request_confirmation(self, prompt: str, meta: dict | None = None) -> bool:
        """Ask the user a yes/no question, reusing the approval future channel.

        Lightweight (no DB approval row) — used by explainable recipe replay to
        confirm a learned shortcut before it has earned silent trust. Times out
        to False (defer to the normal path) so a missing user never blocks.
        """
        self._approval_future = asyncio.get_event_loop().create_future()
        await self._send({
            "type": "approval_request",
            "tool": "recipe_replay",
            "preview": prompt,
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
        result = await _execute_skill(skill, intent.args, self._config)
        await self._send({"type": "tool_result", "tool": intent.capability, "result": result})
        if isinstance(result, dict) and result.get("error"):
            return False  # never answer with a wrong count on error — let the LLM try
        total = _extract_list_count(result)
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

                model_override, provider_override, disable_thinking = _turn_model_overrides(
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
                    on_thinking=_on_thinking,
                    max_tokens=self._response_budget,
                )
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
                    # Fire-and-forget: index this turn into memory
                    self._remember_latest_turn(delivered_text)
                    break

                self.messages.append(message)

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

        if _is_workspace_output_request(latest_user) and len(text) > 500:
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
        except Exception as e:  # noqa: BLE001
            logger.warning("force_final_answer_failed", error=str(e), session_id=self._session_id)

        text = "".join(acc).strip()
        if not text and isinstance(msg, dict):
            text = str(msg.get("content") or "").strip()
        if text:
            await self._deliver_final_content(text)
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
    ) -> tuple[str, dict]:
        """Execute one tool call and return (fn_name, result). Does NOT append to messages."""
        fn = tc.get("function", {})
        fn_name = fn.get("name", "")
        raw_args = fn.get("arguments", {})
        args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")

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

        # Capabilities mode: check gate_actions declared in capabilities.yml
        cap_gate_actions = set()
        if skill:
            cap_gate_actions = set(skill.get("gate_actions") or [])
        action_arg = args.get("action", "")
        if action_arg and action_arg in cap_gate_actions:
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
            return fn_name, result

        if original_name in current_gates:
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
                return fn_name, result

        if skill:
            result = await _execute_skill(skill, args, self._config)
        else:
            available = sorted(self._skill_map.keys())[:30]
            result = {
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
        return fn_name, result

    async def _tool_result_to_history(self, result: dict) -> None:
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
        self.messages.append({
            "role": "tool",
            "content": _trim_tool_result(content_json),
        })

    async def _execute_tools_sequential(
        self, tool_calls: list[dict], iteration: int
    ) -> list[tuple[str, dict]]:
        results: list[tuple[str, dict]] = []
        for tc in tool_calls:
            fn_name, result = await self._execute_single_tool(tc, iteration)
            results.append((fn_name, result))
            await self._tool_result_to_history(result)
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
        for _fn_name, result in results:
            await self._tool_result_to_history(result)
        self._trim_history()
        return list(results)

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

    async def _request_approval(self, skill_name: str, args: dict) -> bool:
        preview = json.dumps(args, ensure_ascii=False, indent=2)

        db_id: str | None = None
        try:
            db_id = await _create_db_approval(skill_name, args)
        except Exception as exc:
            log_degraded("agent_loop.approval_create", exc, skill=skill_name)

        approved = False
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            self._approval_future = asyncio.get_event_loop().create_future()
            await self._send({
                "type": "approval_request",
                "tool": skill_name,
                "args": args,
                "preview": preview,
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
                _load_memory_context(latest_user, self._config),
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


async def _load_memory_context(query: str, config: BuiltinAgentConfig) -> str:
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            resp = await client.post(
                f"{config.backend_url.rstrip('/')}/api/memory/search",
                json={
                    "query": query,
                    "limit": 8,
                    "retrieval_mode": "auto_hybrid",
                    "need_full_coverage": False,
                    "include_explain": False,
                },
                headers=_internal_headers(),
            )
        if resp.status_code >= 400:
            return ""
        hits = resp.json().get("hits") or []
    except Exception as exc:
        log_degraded("agent_loop.memory_search", exc)
        return ""

    lines: list[str] = []
    used_chars = 0
    for index, hit in enumerate(hits, start=1):
        title = str(hit.get("title") or hit.get("kind") or "memory")
        summary = str(hit.get("summary") or "")[:300]
        source = str(hit.get("source") or "memory")
        line = f"{index}. [{source}] {title}: {summary}".strip()
        if used_chars + len(line) > 2400:
            break
        lines.append(line)
        used_chars += len(line)
    return "\n".join(lines)


def _extract_list_count(payload: Any) -> int:
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


async def _create_db_approval(skill_name: str, args: dict) -> str | None:
    """Create an Approval record in DB and return its ID."""
    action_type = _APPROVAL_ACTION_TYPE_MAP.get(skill_name)
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

    entity_type = skill_name.split(".")[0]

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{get_builtin_agent_config().backend_url.rstrip('/')}/api/approvals",
            json={
                "action_type": action_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "requested_by": "sveta",
                "context": args,
            },
            headers=_internal_headers(),
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
            headers=_internal_headers(),
        )
