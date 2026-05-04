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
        return config.system_prompt.strip()
    path = gateway_config.base_prompt_path
    if path.exists():
        raw = path.read_text()
        return raw.replace(
            "[ИНСТРУМЕНТЫ ЗАГРУЖАЮТСЯ АВТОМАТИЧЕСКИ ИЗ РЕЕСТРА SKILLS]", ""
        ).strip()
    agent_name = config.agent_name if config else gateway_config.agent_name
    return f"Ты — AI-ассистент производственного предприятия. Твоё имя: {agent_name}."


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
                    result = resp.json()
                    if isinstance(result, list) and len(result) > 20:
                        result = result[:20]
                    elif isinstance(result, dict) and "items" in result:
                        if isinstance(result["items"], list) and len(result["items"]) > 20:
                            result["items"] = result["items"][:20]
                    return result
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
                    args_dict = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
                    tc_id = tc.get("id") or f"toolu_{name}_{i}"
                    pending_ids.append(tc_id)
                    blocks.append({"type": "tool_use", "id": tc_id, "name": name, "input": args_dict})
                result.append({"role": "assistant", "content": blocks})
            elif content:
                result.append({"role": "assistant", "content": content})

        elif role == "tool":
            tc_id = pending_ids.pop(0) if pending_ids else f"toolu_unknown_{len(pending_results)}"
            pending_results.append({"type": "tool_result", "tool_use_id": tc_id, "content": content})

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
        system_payload = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]

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
                return await _call_ollama_streaming(messages, tools, system_prompt, config, on_token)
            elif p in ("openrouter", "deepseek"):
                return await _call_openai_streaming(messages, tools, system_prompt, config, on_token, provider=p)
            elif p == "anthropic":
                return await _call_anthropic_streaming(messages, tools, system_prompt, config, on_token)
            else:
                logger.warning("unknown_provider_falling_back", provider=p)
                return await _call_ollama_streaming(messages, tools, system_prompt, config, on_token)
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
            top_k=self._config.memory_top_k,
            max_chars=self._config.memory_max_chars,
            retrieval_mode=self._config.memory_mode,
        )

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

    async def on_user_message(self, content: str) -> None:
        await self._init_mcp()
        self.messages.append({"role": "user", "content": content})
        self._trim_history()
        await self._run()

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
                    logger.info("compressing context", session=self._session_id, iteration=iteration)
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
                    await self._send({"type": "text", "content": token})

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
                    # Fire-and-forget: index this turn into memory
                    if self._config.memory_enabled and full_text:
                        latest_user = next(
                            (m.get("content", "") for m in reversed(self.messages) if m.get("role") == "user"),
                            "",
                        )
                        asyncio.create_task(self._memory_mgr.sync_turn(latest_user, full_text))
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
        context = await self._memory_mgr.prefetch(latest_user)
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
                    "limit": config.memory_top_k,
                    "retrieval_mode": config.memory_mode,
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
        if used_chars + len(line) > config.memory_max_chars:
            break
        lines.append(line)
        used_chars += len(line)
    return "\n".join(lines)


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
