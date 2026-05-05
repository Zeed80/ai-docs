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
from app.ai.gateway_config import gateway_config
from app.ai.streaming_scrubber import StreamingContextScrubber

logger = structlog.get_logger()

_OPERATIONAL_POLICY = """
Операционные правила (обязательно):
- Если пользователь просит простую метрику/подсчет ("сколько", "какое количество",
  "всего"), сначала вызови подходящий list/search tool и верни число.
- Не задавай уточняющие вопросы, если сущность уже явно названа пользователем
  (например "счетов" => invoices).
- После уточнения пользователя (например: "только счетов") немедленно выполняй
  tool-вызов без повторного уточнения.
- Для простых запросов не объясняй внутреннюю механику ("мне нужно вызвать
  инструмент"), а сразу выполняй и отвечай результатом.
- Про склад и ТМЦ (остатки, «сколько фрез», «перечисли фрезы»): сразу вызывай
  `warehouse.list_inventory` с `search` по ключевому слову (например `фрез`
  покроет «фреза/фрезы» в названии), без уточняющих вопросов про «типы» или
  «категории».
- Короткие ответы «все», «да», «ок» после вопроса про фрезы — это согласие на
  полный охват; выполняй тот же list/count и не переспрашивай.
- Память всегда автоматическая: не выбирай режим SQL/vector/graph вручную.
  Для запросов по знаниям используй `memory.search`/`memory.explain`
  с `retrieval_mode=auto_hybrid` и `need_full_coverage=true`.
- Если пользователь просит "все", "полный список" или аналогичный полный охват,
  обходи результаты страницами/offset/cursor до исчерпания или до явного
  серверного лимита, а не останавливайся на первой странице.
- Рабочий стол/холст — основной визуальный вывод. Для таблиц, списков,
  графиков, ссылок, изображений и длинных структурированных результатов
  публикуй rich-блок через `canvas.publish`, а в чат давай краткое резюме.
- Чат предназначен только для простых текстовых ответов: число, короткий вывод,
  уточнение, статус выполнения. Не пиши markdown-таблицы в чат.
- Если пользователь просит таблицу, полный список, документ, ссылку, чертёж,
  график, файл, сравнение или большой отчёт — сначала открой Рабочий стол через
  `canvas.publish`, затем в чат напиши одну короткую фразу о том, что показано.
- Табличные шаблоны по умолчанию:
  счета: №, номер, дата, поставщик, сумма, валюта, статус, документ, удалить;
  документы: название, тип, статус, дата, скачать, удалить;
  склад: наименование, SKU, количество, единица, минимум, место хранения;
  поставщики: название, ИНН, trust score, контакт, открытые риски.
- Если пользователь просит изменить таблицу ("добавь столбец", "убери поле",
  "отсортируй", "покажи ещё"), обнови существующий canvas-блок через тот же
  `canvas_id` и `append=false`, не создавай новую чат-таблицу.
- Для каждого выведенного документа, изображения, чертежа или экспортного файла
  указывай доступные действия скачивания и удаления, если API это поддерживает.
""".strip()


def _normalize_ru_yo(text: str) -> str:
    return text.replace("ё", "е").replace("Ё", "Е")


_FREZ_SUBSTRINGS = ("фрез", "endmill", "фреза")


def _agent_canvas_id(kind: str) -> str:
    return f"agent:{kind}"


def _is_workspace_output_request(text: str) -> bool:
    t = _normalize_ru_yo((text or "").lower())
    return any(
        marker in t
        for marker in (
            "таблиц", "полный список", "все списком", "выведи список",
            "покажи список", "ссылк", "документ", "чертеж", "чертёж",
            "график", "диаграм", "excel", "скача", "файл",
        )
    )


def _mentions_invoice_entity(text: str) -> bool:
    t = _normalize_ru_yo((text or "").lower())
    return any(marker in t for marker in ("счет", "счёт", "invoice", "инвойс"))


def _is_invoice_table_request(text: str, prior_user: str | None = None) -> bool:
    t = _normalize_ru_yo((text or "").lower())
    if _mentions_invoice_entity(t) and _is_workspace_output_request(t):
        return True
    if not prior_user or not _mentions_invoice_entity(prior_user):
        return False
    return _is_workspace_output_request(t) and any(
        marker in t
        for marker in ("их", "они", "полный", "таблиц", "список", "все", "всё")
    )


def _mentions_frez_intent(text: str) -> bool:
    t = _normalize_ru_yo(text.lower())
    return any(s in t for s in _FREZ_SUBSTRINGS)


_SHORT_SCOPE_ACK = frozenset({
    "все", "всё", "все.", "всё.", "да", "да.", "ок", "ок.", "ладно", "давай",
    "хорошо", "угу", "ага", "yes", "all",
})


def _is_short_scope_followup(text: str) -> bool:
    t = _normalize_ru_yo(text.strip().lower().rstrip(".!"))
    if not t:
        return False
    if t in _SHORT_SCOPE_ACK:
        return True
    if len(t) <= 4 and t in {"все", "всё", "да", "ок"}:
        return True
    return False


def _frez_inventory_intent(text: str) -> str | None:
    """Return 'count', 'list', or None for the current message (frez-related)."""
    t = _normalize_ru_yo(text.strip().lower())
    if not _mentions_frez_intent(t):
        return None
    list_markers = (
        "перечисли", "перечислить", "список", "покажи", "назови", "выведи",
        "дай список", "выведи список",
        "какие", "какая", "какой", "какое",
    )
    count_markers = ("сколько", "количество", "число", "всего")
    if any(m in t for m in list_markers):
        return "list"
    if any(m in t for m in count_markers):
        return "count"
    if "все" in t or "всё" in t:
        return "list"
    return None


def _frez_followup_intent_from_prior_user_message(prior_user: str) -> str:
    p = _normalize_ru_yo(prior_user.strip().lower())
    if any(m in p for m in ("перечисли", "список", "покажи", "назови", "выведи")):
        return "list"
    if any(m in p for m in ("сколько", "количество", "число")):
        return "count"
    return "count"


def _get_agent_model(config: BuiltinAgentConfig | None = None) -> str:
    """Current agent model: built-in config → ai_settings override → gateway default.

    Built-in agent config is used as primary source because provider/model are
    edited together in the Agent settings UI. ``model_agent`` from ``ai_config``
    remains a backward-compatible fallback and is kept in sync via API handlers.
    """
    if config and config.model:
        return config.model
    try:
        from app.api.ai_settings import get_ai_config

        override = get_ai_config().get("model_agent")
        if override and str(override).strip():
            return str(override).strip()
    except Exception:
        pass
    return gateway_config.reasoning_model


# ── Registry loading ──────────────────────────────────────────────────────────

def _sanitize_name(name: str) -> str:
    """Replace dots with __ for OpenAI-compatible function names."""
    return name.replace(".", "__")


def _load_registry(
    expose_filter: set[str] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Load skills from YAML registry.

    Args:
        expose_filter: if given, only skills in this set are included.
                       Pass None to load ALL skills (used by scenario runner).
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

    try:
        async with httpx.AsyncClient(timeout=float(timeout)) as client:
            if method == "GET":
                resp = await client.get(url, params=query_args)
            elif method == "POST":
                resp = await client.post(url, json=body_args or None)
            elif method == "PATCH":
                resp = await client.patch(url, json=body_args)
            elif method == "DELETE":
                resp = await client.delete(url)
            else:
                return {"error": f"Unsupported method: {method}"}

            if resp.status_code < 400:
                try:
                    return resp.json()
                except Exception:
                    return {"text": resp.text[:2000]}
            else:
                return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:300]}
    except Exception as e:
        return {"error": str(e)}


# ── Ollama client (streaming) ─────────────────────────────────────────────────

async def _call_ollama_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
) -> dict:
    """Stream Ollama response; calls on_token for each text chunk."""
    model = _get_agent_model(config)
    ollama_url = config.ollama_url.rstrip("/")
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "tools": tools,
        "stream": True,
        "options": {"temperature": config.temperature},
    }
    if config.disable_thinking:
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

def _openrouter_base_url() -> str:
    return "https://openrouter.ai/api/v1"


def _deepseek_base_url() -> str:
    return "https://api.deepseek.com/v1"


async def _call_openai_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    provider: str,
) -> dict:
    """Stream an OpenAI-compatible SSE endpoint (OpenRouter, DeepSeek).

    Returns a normalised message dict identical to the Ollama format so that
    the rest of AgentSession._run() needs no changes.
    """
    model = _get_agent_model(config)

    if provider == "openrouter":
        base_url = _openrouter_base_url()
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        extra: dict[str, str] = {
            "HTTP-Referer": "https://ai-workspace.local",
            "X-Title": "AI Manufacturing Workspace",
        }
    elif provider == "deepseek":
        base_url = _deepseek_base_url()
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        extra = {}
    else:
        raise ValueError(f"Unsupported openai-compatible provider: {provider}")

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}", **extra}

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "stream": True,
        "temperature": config.temperature,
    }
    if tools:
        payload["tools"] = tools
    if config.disable_thinking:
        # OpenAI-compatible providers may honor this to suppress reasoning traces
        # and force concise direct outputs on "thinking" models.
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
        "max_tokens": 4096,
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

async def _call_provider_streaming(
    messages: list[dict],
    tools: list[dict],
    system_prompt: str | None,
    config: BuiltinAgentConfig,
    on_token: Callable[[str], Awaitable[None]],
    model_override: str | None = None,
) -> dict:
    """Dispatch to the configured LLM provider with optional fallback chain."""
    providers_to_try = [config.provider or "ollama"] + list(config.fallback_providers or [])

    last_exc: Exception | None = None
    for p in providers_to_try:
        try:
            if p == "ollama":
                return await _call_ollama_streaming(
                    messages, tools, system_prompt, config, on_token
                )
            elif p in ("openrouter", "deepseek"):
                return await _call_openai_streaming(
                    messages, tools, system_prompt, config, on_token, provider=p
                )
            elif p == "anthropic":
                return await _call_anthropic_streaming(
                    messages, tools, system_prompt, config, on_token
                )
            else:
                logger.warning("unknown_provider_falling_back", provider=p)
                return await _call_ollama_streaming(
                    messages, tools, system_prompt, config, on_token
                )
        except Exception as exc:
            last_exc = exc
            logger.warning("provider_call_failed_trying_fallback", provider=p, error=str(exc))

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

        self._config = get_builtin_agent_config()
        self._rebuild_runtime_components(self._config)
        self._mcp_initialised = False

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

        await _call_provider_streaming(messages, [], None, config, _collect)
        for chunk in accumulated:
            yield chunk

    async def _log_action(self, **kwargs: Any) -> None:
        """Persist agent step to DB (fire-and-forget)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self._config.backend_url.rstrip('/')}/api/agent-actions",
                    json={"session_id": self._session_id, **kwargs},
                )
        except Exception:
            pass

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

    def _rebuild_runtime_components(self, config: BuiltinAgentConfig) -> None:
        """Rebuild tools/system/dependencies when runtime agent config changes."""
        self._config = config
        exposed = set(self._config.exposed_skills)
        self._tools, self._skill_map = _load_registry(
            expose_filter=exposed if exposed else None
        )
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
        )
        # Re-init MCP tools with updated server config on next message.
        self._mcp_initialised = False

    def _refresh_runtime_config(self) -> None:
        """Reload config so model/skills changes apply without reconnect."""
        latest_config = get_builtin_agent_config()
        if latest_config.model_dump(mode="json") == self._config.model_dump(mode="json"):
            return
        self._rebuild_runtime_components(latest_config)
        logger.info(
            "agent_runtime_config_reloaded",
            model=_get_agent_model(self._config),
            provider=self._config.provider,
            exposed_skills=len(self._config.exposed_skills),
        )

    async def on_user_message(self, content: str) -> None:
        self._refresh_runtime_config()
        await self._init_mcp()
        self.messages.append({"role": "user", "content": content})
        self._trim_history()
        if await self._try_handle_simple_count_query(content):
            return
        if await self._try_handle_invoice_table_query(content):
            return
        if await self._try_handle_frez_inventory_query(content):
            return
        await self._run()

    async def _try_handle_simple_count_query(self, content: str) -> bool:
        text = (content or "").strip().lower()
        if not text:
            return False

        is_count_intent = any(token in text for token in ("сколько", "количество", "всего"))
        is_invoices_only = any(token in text for token in ("счет", "счёт", "invoice"))
        force_invoices = "только счет" in text or "только счёт" in text
        if not ((is_count_intent and is_invoices_only) or force_invoices):
            return False

        # Prefer invoice.list for invoice count questions.
        invoice_skill = self._skill_map.get("invoice__list")
        if invoice_skill:
            args: dict[str, Any] = {}
            await self._send({"type": "tool_call", "tool": "invoice__list", "args": args})
            result = await _execute_skill(invoice_skill, args, self._config)
            await self._send({"type": "tool_result", "tool": "invoice__list", "result": result})
            total = _extract_list_count(result)
            await self._send({
                "type": "text",
                "content": f"Всего загружено счетов: {total}.",
            })
            await self._send({"type": "done"})
            return True

        # Hard fallback: direct backend API call when invoice.list skill is not exposed.
        total = await _fetch_invoice_count_direct(self._config)
        if total is not None:
            await self._send({
                "type": "text",
                "content": f"Всего загружено счетов: {total}.",
            })
            await self._send({"type": "done"})
            return True

        # Fallback: count documents if invoice.list is unavailable.
        doc_skill = self._skill_map.get("doc__list")
        if doc_skill:
            args = {}
            await self._send({"type": "tool_call", "tool": "doc__list", "args": args})
            result = await _execute_skill(doc_skill, args, self._config)
            await self._send({"type": "tool_result", "tool": "doc__list", "result": result})
            total = _extract_list_count(result)
            await self._send({
                "type": "text",
                "content": f"Сейчас в системе документов: {total}.",
            })
            await self._send({"type": "done"})
            return True

        return False

    async def _publish_canvas(
        self,
        block: dict[str, Any],
        *,
        canvas_id: str | None = None,
        append: bool = True,
    ) -> None:
        await self._send({
            "type": "canvas",
            "canvas_id": canvas_id,
            "block": block,
            "append": append,
        })

    async def _try_handle_invoice_table_query(self, content: str) -> bool:
        prior_users = [
            str(m.get("content", ""))
            for m in self.messages[:-1]
            if m.get("role") == "user"
        ]
        prior_user = prior_users[-1] if prior_users else None
        if not _is_invoice_table_request(content, prior_user):
            return False

        skill_name = "workspace__invoice_table"
        args = {
            "canvas_id": _agent_canvas_id("invoice-list"),
            "limit": 5000,
            "include_delete_actions": True,
        }
        await self._send({
            "type": "tool_call",
            "tool": skill_name,
            "args": args,
        })
        skill = self._skill_map.get(skill_name)
        if skill:
            data = await _execute_skill(skill, args, self._config)
        else:
            data = await _publish_invoice_table_direct(self._config, args)
            if data is None:
                return False
        await self._send({"type": "tool_result", "tool": skill_name, "result": data})
        total = int(data.get("total") or 0) if isinstance(data, dict) else 0
        shown = int(data.get("shown") or total) if isinstance(data, dict) else total
        message = str(data.get("message") or "") if isinstance(data, dict) else ""
        await self._send({
            "type": "text",
            "content": (
                message
                or f"Открыл на Рабочем столе таблицу со счетами: {shown} из {total}."
            ),
        })
        await self._send({"type": "done"})
        return True

    async def _try_handle_frez_inventory_query(self, content: str) -> bool:
        """Fast-path for warehouse cutter/mill (фреза) queries — avoids clarification loops."""
        raw = (content or "").strip()
        if not raw:
            return False

        tl = _normalize_ru_yo(raw.lower())
        intent = _frez_inventory_intent(raw)

        if intent is None and _is_short_scope_followup(tl):
            prior_users = [
                str(m.get("content", ""))
                for m in self.messages[:-1]
                if m.get("role") == "user"
            ]
            if not prior_users:
                return False
            prev_u = prior_users[-1]
            if not _mentions_frez_intent(prev_u):
                return False
            intent = _frez_followup_intent_from_prior_user_message(prev_u)

        if intent is None:
            return False

        search_q = "фрез"
        skill = self._skill_map.get("warehouse__list_inventory")
        page_size = 200
        max_items = 1000
        total = 0
        items: list[Any] = []
        offset = 0
        while offset < max_items:
            args: dict[str, Any] = {"search": search_q, "limit": page_size, "offset": offset}
            if skill:
                await self._send({
                    "type": "tool_call",
                    "tool": "warehouse__list_inventory",
                    "args": args,
                })
                result = await _execute_skill(skill, args, self._config)
                await self._send({
                    "type": "tool_result",
                    "tool": "warehouse__list_inventory",
                    "result": result,
                })
            else:
                result = await _fetch_inventory_search_direct(
                    self._config,
                    search=search_q,
                    limit=page_size,
                    offset=offset,
                )
                if result is None:
                    return False
            if offset == 0:
                total = _extract_list_count(result)
            page_items: list[Any] = []
            if isinstance(result, dict) and isinstance(result.get("items"), list):
                page_items = result["items"]
            items.extend(page_items)
            if not page_items or len(items) >= total or len(page_items) < page_size:
                break
            offset += page_size

        if intent == "count":
            await self._send({
                "type": "text",
                "content": f"Позиций на складе с «{search_q}» в наименовании "
                f"(поиск по названию): **{total}**.",
            })
            await self._send({"type": "done"})
            return True

        rows = []
        for idx, it in enumerate(items[:max_items], start=1):
            if not isinstance(it, dict):
                continue
            rows.append({
                "index": idx,
                "name": it.get("name"),
                "sku": it.get("sku"),
                "current_qty": it.get("current_qty"),
                "unit": it.get("unit"),
                "min_qty": it.get("min_qty"),
                "location": it.get("location"),
            })

        await self._publish_canvas(
            {
                "type": "table",
                "title": f"Склад: позиции по запросу «{search_q}» ({total})",
                "columns": [
                    {"key": "index", "header": "№", "type": "number", "width": 56},
                    {"key": "name", "header": "Наименование", "type": "text"},
                    {"key": "sku", "header": "SKU", "type": "text"},
                    {"key": "current_qty", "header": "Количество", "type": "number"},
                    {"key": "unit", "header": "Ед.", "type": "text", "width": 64},
                    {"key": "min_qty", "header": "Минимум", "type": "number"},
                    {"key": "location", "header": "Место", "type": "text"},
                ],
                "rows": rows,
            },
            canvas_id=_agent_canvas_id("warehouse-frez"),
            append=False,
        )
        await self._send({
            "type": "text",
            "content": f"Открыл на Рабочем столе таблицу позиций склада: {len(rows)} из {total}.",
        })
        await self._send({"type": "done"})
        return True

    async def on_approval(self, approved: bool) -> None:
        if self._approval_future and not self._approval_future.done():
            self._approval_future.set_result(approved)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            if not self._config.enabled:
                await self._send({
                    "type": "error",
                    "content": "Встроенный агент отключен в настройках.",
                })
                return

            await self._append_memory_context()

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

                message = await _call_provider_streaming(
                    self.messages, self._tools, self._system, self._config, on_token
                )
                duration_ms = int((time.time() - t_start) * 1000)
                tool_calls = message.get("tool_calls") or []
                full_text = "".join(accumulated_text)

                asyncio.create_task(self._log_action(
                    iteration=iteration,
                    action_type="llm_call",
                    content_text=full_text[:2000] if full_text else None,
                    model_name=_get_agent_model(self._config),
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
                    if self._config.memory_enabled and delivered_text:
                        latest_user = next(
                            (
                                m.get("content", "")
                                for m in reversed(self.messages)
                                if m.get("role") == "user"
                            ),
                            "",
                        )
                        asyncio.create_task(
                            self._memory_mgr.sync_turn(latest_user, delivered_text)
                        )
                    break

                self.messages.append(message)

                from app.ai.tool_parallelism import should_parallelize
                if should_parallelize(tool_calls):
                    await self._execute_tools_parallel(tool_calls, iteration)
                else:
                    await self._execute_tools_sequential(tool_calls, iteration)

        except Exception as e:
            logger.error("agent_loop_error", error=str(e))
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

        if original_name in self._approval_gates:
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
            result = {"error": f"Unknown skill: {fn_name}"}

        asyncio.create_task(self._log_action(
            iteration=iteration,
            action_type="tool_result",
            tool_name=fn_name,
            tool_result=result if len(str(result)) < 2000 else {"truncated": True},
        ))
        await self._send({"type": "tool_result", "tool": fn_name, "result": result})
        return fn_name, result

    async def _execute_tools_sequential(
        self, tool_calls: list[dict], iteration: int
    ) -> None:
        for tc in tool_calls:
            fn_name, result = await self._execute_single_tool(tc, iteration)
            self.messages.append({
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False),
            })
            self._trim_history()

    async def _execute_tools_parallel(
        self, tool_calls: list[dict], iteration: int
    ) -> None:
        results = await asyncio.gather(
            *[self._execute_single_tool(tc, iteration) for tc in tool_calls],
            return_exceptions=False,
        )
        for _fn_name, result in results:
            self.messages.append({
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False),
            })
        self._trim_history()

    async def _request_approval(self, skill_name: str, args: dict) -> bool:
        preview = json.dumps(args, ensure_ascii=False, indent=2)

        db_id: str | None = None
        try:
            db_id = await _create_db_approval(skill_name, args)
        except Exception:
            pass

        self._approval_future = asyncio.get_event_loop().create_future()
        await self._send({
            "type": "approval_request",
            "tool": skill_name,
            "args": args,
            "preview": preview,
            "db_id": db_id,
        })
        try:
            approved = await asyncio.wait_for(
                self._approval_future,
                timeout=float(self._config.approval_timeout_seconds),
            )
        except TimeoutError:
            approved = False
        finally:
            self._approval_future = None

        if db_id:
            try:
                await _decide_db_approval(db_id, approved)
            except Exception:
                pass

        return approved

    def _trim_history(self) -> None:
        keep = self._config.max_history_messages
        if len(self.messages) > keep:
            self.messages = self.messages[-keep:]

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
        context = await self._memory_mgr.prefetch(latest_user, session_id=self._session_id)
        if not context:
            # Fall back to existing HTTP search if MemoryManager returned nothing
            context = await _load_memory_context(latest_user, self._config)
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
        self._trim_history()


async def _load_memory_context(query: str, config: BuiltinAgentConfig) -> str:
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            resp = await client.post(
                f"{config.backend_url.rstrip('/')}/api/memory/search",
                json={
                    "query": query,
                    "limit": 120,
                    "retrieval_mode": "auto_hybrid",
                    "need_full_coverage": True,
                    "include_explain": False,
                },
            )
        if resp.status_code >= 400:
            return ""
        hits = resp.json().get("hits") or []
    except Exception:
        return ""

    lines: list[str] = []
    used_chars = 0
    for index, hit in enumerate(hits, start=1):
        title = str(hit.get("title") or hit.get("kind") or "memory")
        summary = str(hit.get("summary") or "")[:1200]
        source = str(hit.get("source") or "memory")
        line = f"{index}. [{source}] {title}: {summary}".strip()
        if used_chars + len(line) > 20000:
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


async def _fetch_invoice_count_direct(config: BuiltinAgentConfig) -> int | None:
    url = f"{config.backend_url.rstrip('/')}/api/invoices"
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return _extract_list_count(data)
    except Exception:
        return None


async def _publish_invoice_table_direct(
    config: BuiltinAgentConfig,
    args: dict[str, Any],
) -> dict[str, Any] | None:
    url = f"{config.backend_url.rstrip('/')}/api/workspace/agent/invoices/table"
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            resp = await client.post(url, json=args)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _fetch_invoices_direct(
    config: BuiltinAgentConfig,
    *,
    limit: int = 200,
    max_items: int = 5000,
) -> dict[str, Any] | None:
    url = f"{config.backend_url.rstrip('/')}/api/invoices"
    items: list[dict[str, Any]] = []
    total = 0
    offset = 0
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            while offset < max_items:
                resp = await client.get(url, params={"limit": limit, "offset": offset})
                if resp.status_code >= 400:
                    return None
                data = resp.json()
                if not isinstance(data, dict):
                    return None
                if offset == 0:
                    total = _extract_list_count(data)
                page_items = data.get("items")
                if not isinstance(page_items, list):
                    page_items = []
                items.extend([item for item in page_items if isinstance(item, dict)])
                if not page_items or len(items) >= total or len(page_items) < limit:
                    break
                offset += limit
    except Exception:
        return None
    return {"items": items, "total": total or len(items), "offset": 0, "limit": len(items)}


def _format_date(value: Any) -> str:
    if not value:
        return ""
    text = str(value)
    if "T" in text:
        text = text.split("T", 1)[0]
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return f"{text[8:10]}.{text[5:7]}.{text[0:4]}"
    return text


def _format_money(value: Any) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.2f}".replace(",", " ").replace(".00", "")


def _invoice_canvas_row(item: dict[str, Any], index: int) -> dict[str, Any]:
    invoice_id = str(item.get("id") or "")
    document_id = str(item.get("document_id") or "")
    supplier = item.get("supplier")
    supplier_name = ""
    if isinstance(supplier, dict):
        supplier_name = str(supplier.get("name") or "")
    return {
        "index": index,
        "id": invoice_id,
        "document_id": document_id,
        "invoice_number": item.get("invoice_number") or "",
        "invoice_date": _format_date(item.get("invoice_date")),
        "supplier": supplier_name,
        "total_amount": _format_money(item.get("total_amount")),
        "currency": item.get("currency") or "RUB",
        "status": item.get("status") or "",
        "document_download": {
            "href": f"/api/documents/{document_id}/download",
            "label": "Скачать",
        } if document_id else None,
        "invoice_delete": {
            "href": f"/api/invoices/{invoice_id}",
            "label": "Удалить",
            "confirm": f"Удалить счет {item.get('invoice_number') or invoice_id}?",
            "method": "DELETE",
        } if invoice_id else None,
        "document_delete": {
            "href": f"/api/documents/{document_id}",
            "label": "Удалить",
            "confirm": f"Удалить документ счета {item.get('invoice_number') or invoice_id}?",
            "method": "DELETE",
        } if document_id else None,
    }


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


async def _fetch_inventory_search_direct(
    config: BuiltinAgentConfig,
    *,
    search: str,
    limit: int,
    offset: int,
) -> dict[str, Any] | None:
    url = f"{config.backend_url.rstrip('/')}/api/warehouse/inventory"
    try:
        async with httpx.AsyncClient(timeout=float(config.backend_timeout_seconds)) as client:
            resp = await client.get(
                url,
                params={"search": search, "limit": limit, "offset": offset},
            )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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
        )
