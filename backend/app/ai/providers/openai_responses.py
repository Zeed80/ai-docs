"""OpenAI Responses API provider (ChatGPT/Codex sign-in, xAI SuperGrok sign-in).

Nodes signed in through the provider's site (see app/ai/provider_oauth.py) are
called through the Responses API rather than Chat Completions: the ChatGPT
subscription only exists behind the Codex backend
(chatgpt.com/backend-api/codex/responses), and xAI's subscription sign-in is
used with api.x.ai/v1/responses.

The Codex backend accepts streamed requests only and requires
``store: false`` and a non-empty ``instructions`` field, so every request here
is streamed and the events are folded back into one answer. The conversion
helpers are shared with the agent loop (agent_loop._call_responses_streaming).
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.ai.providers.base import AIProvider
from app.ai.providers.openai_compatible import (
    OpenAICompatibleProvider,
    _raise_for_status,
    _requested_schema,
)
from app.ai.schemas import AIRequest, AIResponse, AIUsage, ProposedToolCall

DEFAULT_INSTRUCTIONS = "You are a helpful assistant."


def responses_url(base_url: str) -> str:
    return f"{str(base_url).rstrip('/')}/responses"


def _text_part(role: str, text: str) -> dict:
    return {"type": "output_text" if role == "assistant" else "input_text", "text": text}


def chat_to_responses(messages: list[dict]) -> tuple[str, list[dict]]:
    """Chat Completions messages → (instructions, Responses input items)."""
    instructions: list[str] = []
    items: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions.append(content)
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": str(m.get("tool_call_id") or ""),
                    "output": content if isinstance(content, str) else json.dumps(content),
                }
            )
            continue
        parts: list[dict] = []
        if isinstance(content, str):
            if content:
                parts.append(_text_part(role, content))
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    parts.append(_text_part(role, part.get("text") or ""))
                elif part.get("type") == "image_url" and role != "assistant":
                    url = (part.get("image_url") or {}).get("url")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
        if m.get("images") and role == "user":  # Ollama-style raw base64 images
            for img in m["images"]:
                url = img if str(img).startswith("data:") else f"data:image/png;base64,{img}"
                parts.append({"type": "input_image", "image_url": url})
        if parts:
            items.append({"role": "assistant" if role == "assistant" else "user", "content": parts})
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            items.append(
                {
                    "type": "function_call",
                    "call_id": str(call.get("id") or ""),
                    "name": fn.get("name") or "",
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}),
                }
            )
    return "\n\n".join(instructions) or DEFAULT_INSTRUCTIONS, items


def chat_tools_to_responses(tools: list[dict]) -> list[dict]:
    out = []
    for tool in tools or []:
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not fn or not fn.get("name"):
            continue
        out.append(
            {
                "type": "function",
                "name": fn["name"],
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


class ResponsesStream:
    """Folds Responses API SSE events into text, reasoning and tool calls."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[str, dict] = {}
        self.usage: dict = {}
        self.error: str | None = None

    def feed(self, event: dict) -> tuple[str | None, str | None]:
        """Apply one event; returns (text delta, reasoning delta) for streaming callers."""
        etype = event.get("type") or ""
        if etype == "response.output_text.delta":
            delta = event.get("delta") or ""
            self.text.append(delta)
            return delta, None
        if etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            delta = event.get("delta") or ""
            self.reasoning.append(delta)
            return None, delta
        if etype == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                self.calls[item.get("call_id") or item.get("id") or str(len(self.calls))] = item
            elif item.get("type") == "message" and not self.text:
                for part in item.get("content") or []:
                    if part.get("type") == "output_text":
                        self.text.append(part.get("text") or "")
        elif etype in ("response.completed", "response.done", "response.incomplete"):
            self.usage = (event.get("response") or {}).get("usage") or {}
        elif etype in ("response.failed", "error"):
            err = (event.get("response") or {}).get("error") or event.get("error") or event
            self.error = err.get("message") if isinstance(err, dict) else str(err)
        return None, None

    def tool_calls(self) -> list[dict]:
        return [
            {
                "id": cid,
                "type": "function",
                "function": {"name": c.get("name") or "", "arguments": c.get("arguments") or "{}"},
            }
            for cid, c in self.calls.items()
        ]


async def iter_sse(response: httpx.Response):
    """Parse ``data: {...}`` SSE lines into dicts."""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except ValueError:
            continue


def build_body(
    *,
    model: str,
    instructions: str,
    items: list[dict],
    tools: list[dict] | None = None,
    max_output_tokens: int | None = None,
    reasoning_effort: str | None = None,
    text_format: dict | None = None,
    codex: bool = False,
) -> dict:
    body: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": items,
        "store": False,
        "stream": True,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
        body["parallel_tool_calls"] = False
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if text_format:
        body["text"] = {"format": text_format}
    # The Codex backend rejects max_output_tokens; the public Responses API takes it.
    if max_output_tokens and not codex:
        body["max_output_tokens"] = int(max_output_tokens)
    return body


def is_codex(base_url: str) -> bool:
    return "chatgpt.com/backend-api/codex" in str(base_url)


class OpenAIResponsesProvider(AIProvider):
    """Router provider for Responses-API nodes (signed in through the provider's site)."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self.kind = config.kind

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self.config.api_key or ''}",
            **(self.config.extra_headers or {}),
        }

    async def _run(self, request: AIRequest, model: str, messages: list[dict]) -> AIResponse:
        started = time.perf_counter()
        instructions, items = chat_to_responses(messages)
        schema = _requested_schema(request)
        text_format = (
            {"type": "json_schema", "name": "answer", "schema": schema, "strict": False}
            if isinstance(schema, dict)
            else None
        )
        tools = [
            {"type": "function", "name": t.name, "description": t.description,
             "parameters": t.input_schema}
            for t in (request.tools or [])
        ]  # fmt: skip
        effort = request.thinking_level if request.thinking else None
        body = build_body(
            model=model,
            instructions=instructions,
            items=items,
            tools=tools,
            max_output_tokens=(request.metadata or {}).get("num_predict"),
            reasoning_effort=effort,
            text_format=text_format,
            codex=is_codex(self.config.base_url),
        )
        stream = ResponsesStream()
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            async with client.stream(
                "POST", responses_url(self.config.base_url), headers=self._headers(), json=body
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    _raise_for_status(resp)
                async for event in iter_sse(resp):
                    stream.feed(event)
        if stream.error:
            raise RuntimeError(f"Responses API: {stream.error}")
        usage = stream.usage
        return AIResponse(
            task=request.task,
            provider=self.kind,
            model=model,
            text="".join(stream.text),
            proposed_tool_calls=[
                ProposedToolCall(
                    name=c["function"]["name"],
                    arguments=_loads(c["function"]["arguments"]),
                )
                for c in stream.tool_calls()
            ],
            usage=AIUsage(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                total_tokens=usage.get("total_tokens"),
                latency_ms=int((time.perf_counter() - started) * 1000),
            ),
            raw={"usage": usage},
        )

    async def chat(self, request: AIRequest, model: str) -> AIResponse:
        messages = OpenAICompatibleProvider(self.config)._messages(request)
        return await self._run(request, model, messages)

    async def vision(self, request: AIRequest, model: str) -> AIResponse:
        system = ""
        texts: list[str] = []
        for m in request.messages:
            if m.role == "system":
                system = m.content
            elif m.role in ("user", "assistant"):
                texts.append(m.content)
        question = "\n\n".join(texts) or request.prompt or request.input_text or ""
        content: list[dict] = [{"type": "text", "text": question}]
        for image in request.images:
            url = image if image.startswith(("data:", "http")) else f"data:image/png;base64,{image}"
            content.append({"type": "image_url", "image_url": {"url": url}})
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": content}
        ]
        return await self._run(request, model, messages)

    async def embedding(self, request: AIRequest, model: str) -> AIResponse:
        raise NotImplementedError("Responses API nodes do not serve embeddings")


def _loads(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}
