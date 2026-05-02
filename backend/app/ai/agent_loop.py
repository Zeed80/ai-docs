"""Built-in agent loop — Ollama tool calling via /api/chat."""

from __future__ import annotations

import asyncio
import json
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
    """Current agent model: built-in config → ai_settings override → gateway default."""
    if config and config.model:
        return config.model
    try:
        from app.api.ai_settings import get_ai_config

        override = get_ai_config().get("model_agent")
        if override:
            return override
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

    async def on_user_message(self, content: str) -> None:
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

            for iteration in range(self._config.max_steps):
                self._iteration = iteration

                t_start = time.time()
                accumulated_text: list[str] = []

                async def on_token(token: str) -> None:
                    accumulated_text.append(token)
                    await self._send({"type": "text", "content": token})

                message = await _call_ollama_streaming(
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
                    break

                self.messages.append(message)

                for tc in tool_calls:
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
                            result = {"status": "rejected", "message": "Отклонено пользователем"}
                            self.messages.append({
                                "role": "tool",
                                "content": json.dumps(result, ensure_ascii=False),
                            })
                            await self._send({
                                "type": "tool_result",
                                "tool": fn_name,
                                "result": result,
                            })
                            continue

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
                    self.messages.append({
                        "role": "tool",
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                    self._trim_history()

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
