"""MCP (Model Context Protocol) client for AgentSession.

Connects to external MCP servers via stdio subprocess or HTTP/StreamableHTTP
and exposes their tools alongside built-in skills.

Two transports are supported:
  - stdio: launch a local command, communicate over stdin/stdout JSON-RPC
  - http:  connect to a running MCP server via HTTP (StreamableHTTP or plain)

Each MCPServerClient runs in a background asyncio task with auto-reconnect
(exponential backoff, max 5 retries).

Usage::

    servers = [
        {"name": "filesystem", "transport": "stdio",
         "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"]},
        {"name": "postgres", "transport": "http",
         "url": "http://localhost:5173"},
    ]
    tools, handlers = await load_mcp_tools(servers)
    # tools  → list[dict]  OpenAI function-calling schema
    # handlers → dict[sanitised_name → async callable]
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_BACKOFF_BASE = 2.0  # seconds


def _sanitize(name: str) -> str:
    return name.replace(".", "__").replace("-", "_").replace(" ", "_")


# ── Stdio transport ───────────────────────���───────────────────────────────────

class StdioMCPClient:
    """Connects to an MCP server as a subprocess (stdio JSON-RPC)."""

    def __init__(self, name: str, command: str, args: list[str]) -> None:
        self.name = name
        self._command = command
        self._args = args
        self._proc: asyncio.subprocess.Process | None = None
        self._tools: list[dict] = []
        self._ready = asyncio.Event()

    async def start(self) -> None:
        cmd = shutil.which(self._command) or self._command
        self._proc = await asyncio.create_subprocess_exec(
            cmd, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self._initialize()

    async def _rpc(self, method: str, params: dict | None = None) -> Any:
        if not self._proc or self._proc.stdin is None:
            raise RuntimeError("MCP subprocess not started")
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        self._proc.stdin.write((req + "\n").encode())
        await self._proc.stdin.drain()
        raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=30.0)
        return json.loads(raw)

    async def _initialize(self) -> None:
        resp = await self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "sveta-agent", "version": "1.0"},
        })
        if resp.get("error"):
            raise RuntimeError(f"MCP init error: {resp['error']}")
        await self._rpc("notifications/initialized", {})
        await self._list_tools()
        self._ready.set()

    async def _list_tools(self) -> None:
        resp = await self._rpc("tools/list")
        self._tools = resp.get("result", {}).get("tools") or []

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        resp = await self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
        if resp.get("error"):
            return {"error": resp["error"]}
        content = resp.get("result", {}).get("content") or []
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return {"result": "\n".join(texts)} if texts else resp.get("result", {})

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                pass


# ── HTTP transport ────────────────────────────────────────────────────────────

class HttpMCPClient:
    """Connects to a running MCP server over HTTP."""

    def __init__(self, name: str, url: str) -> None:
        self.name = name
        self._url = url.rstrip("/")
        self._tools: list[dict] = []
        self._ready = asyncio.Event()

    async def start(self) -> None:
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._url}/mcp",
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "sveta-agent", "version": "1.0"},
                    },
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(f"MCP HTTP init error: {data['error']}")
            # list tools
            resp2 = await client.post(
                f"{self._url}/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={"Content-Type": "application/json"},
            )
            resp2.raise_for_status()
            self._tools = resp2.json().get("result", {}).get("tools") or []
        self._ready.set()

    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        import httpx
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self._url}/mcp",
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                return {"error": data["error"]}
            content = data.get("result", {}).get("content") or []
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return {"result": "\n".join(texts)} if texts else data.get("result", {})

    @property
    def tools(self) -> list[dict]:
        return self._tools

    async def stop(self) -> None:
        pass


# ── Public API ──────────────────────────────────────────────────────���─────────

async def _start_with_retry(client: StdioMCPClient | HttpMCPClient) -> bool:
    """Try to start client with exponential backoff. Return True on success."""
    for attempt in range(_MAX_RETRIES):
        try:
            await client.start()
            logger.info("MCP server connected: %s", client.name)
            return True
        except Exception as exc:
            wait = _BACKOFF_BASE ** attempt
            logger.warning(
                "MCP server '%s' failed (attempt %d/%d): %s — retrying in %.1fs",
                client.name, attempt + 1, _MAX_RETRIES, exc, wait,
            )
            await asyncio.sleep(wait)
    logger.error("MCP server '%s' failed after %d attempts — skipping", client.name, _MAX_RETRIES)
    return False


def _mcp_tool_to_openai(tool: dict, server_prefix: str) -> dict:
    """Convert an MCP tool descriptor to OpenAI function-calling schema."""
    raw_name = tool.get("name", "unknown")
    fn_name = _sanitize(f"mcp_{server_prefix}_{raw_name}")
    return {
        "type": "function",
        "function": {
            "name": fn_name,
            "description": tool.get("description", ""),
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


# ── Built-in MCP tools (wrap internal FastAPI endpoints) ──────────────────────

_BUILTIN_TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "drawing_analysis_mcp",
            "description": (
                "Запустить или получить результат AI-анализа чертежа. "
                "Возвращает структурированный результат: штамп, список конструктивных элементов "
                "(отверстия, карманы, поверхности и др.) с размерами, допусками, шероховатостью."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "drawing_id": {
                        "type": "string",
                        "description": "UUID чертежа для анализа",
                    },
                    "include_dimensions": {
                        "type": "boolean",
                        "description": "Включить размеры и допуски в результат (по умолчанию true)",
                        "default": True,
                    },
                    "include_surfaces": {
                        "type": "boolean",
                        "description": "Включить шероховатость поверхностей (по умолчанию true)",
                        "default": True,
                    },
                    "include_gdt": {
                        "type": "boolean",
                        "description": "Включить допуски формы и расположения GD&T (по умолчанию true)",
                        "default": True,
                    },
                    "reanalyze": {
                        "type": "boolean",
                        "description": "Перезапустить AI-анализ (по умолчанию false — вернуть кэш)",
                        "default": False,
                    },
                },
                "required": ["drawing_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tool_search_mcp",
            "description": (
                "Семантический поиск режущих инструментов в базе данных поставщиков. "
                "Поддерживает поиск по тексту и фильтрацию по параметрам: тип, диаметр, материал. "
                "Возвращает список инструментов с ценами и параметрами."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Текстовый запрос, например 'сверло Ø10 для нержавеющей стали'",
                    },
                    "tool_type": {
                        "type": "string",
                        "description": "Тип инструмента: drill, endmill, insert, holder, tap, reamer, boring_bar, saw, other",
                        "enum": ["drill", "endmill", "insert", "holder", "tap", "reamer", "boring_bar", "saw", "other"],
                    },
                    "diameter_min": {
                        "type": "number",
                        "description": "Минимальный диаметр в мм",
                    },
                    "diameter_max": {
                        "type": "number",
                        "description": "Максимальный диаметр в мм",
                    },
                    "material": {
                        "type": "string",
                        "description": "Материал инструмента, например 'HSS', 'carbide', 'HSS-Co'",
                    },
                    "supplier_id": {
                        "type": "string",
                        "description": "UUID поставщика для фильтрации по конкретному каталогу",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Максимальное количество результатов (по умолчанию 10)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
]


async def _handle_drawing_analysis_mcp(args: dict) -> dict:
    """Call internal /drawings/{id} and optionally /drawings/{id}/reanalyze."""
    import httpx
    from app.core.config import settings

    drawing_id = args.get("drawing_id")
    if not drawing_id:
        return {"error": "drawing_id is required"}

    reanalyze = args.get("reanalyze", False)
    base_url = f"http://localhost:{getattr(settings, 'PORT', 8000)}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        if reanalyze:
            await client.post(f"{base_url}/drawings/{drawing_id}/reanalyze")

        resp = await client.get(f"{base_url}/drawings/{drawing_id}")
        resp.raise_for_status()
        drawing = resp.json()

        features_resp = await client.get(f"{base_url}/drawings/{drawing_id}/features")
        features_resp.raise_for_status()
        features_raw = features_resp.json()

    include_dimensions = args.get("include_dimensions", True)
    include_surfaces = args.get("include_surfaces", True)
    include_gdt = args.get("include_gdt", True)

    features_out = []
    for f in features_raw:
        entry: dict[str, Any] = {
            "id": f.get("id"),
            "feature_type": f.get("feature_type"),
            "name": f.get("name"),
            "description": f.get("description"),
            "confidence": f.get("confidence"),
            "reviewed_at": f.get("reviewed_at"),
        }
        if include_dimensions:
            entry["dimensions"] = f.get("dimensions", [])
        if include_surfaces:
            entry["surfaces"] = f.get("surfaces", [])
        if include_gdt:
            entry["gdt"] = f.get("gdt", [])
        entry["tool_binding"] = f.get("tool_binding")
        features_out.append(entry)

    return {
        "drawing": {
            "id": drawing.get("id"),
            "filename": drawing.get("filename"),
            "format": drawing.get("format"),
            "status": drawing.get("status"),
            "title_block": drawing.get("title_block"),
        },
        "features": features_out,
        "total_features": len(features_out),
    }


async def _handle_tool_search_mcp(args: dict) -> dict:
    """Call internal /tool-catalog/search endpoint."""
    import httpx
    from app.core.config import settings

    query = args.get("query", "")
    if not query:
        return {"error": "query is required"}

    base_url = f"http://localhost:{getattr(settings, 'PORT', 8000)}"
    params: dict[str, Any] = {"q": query, "limit": args.get("limit", 10)}
    if args.get("tool_type"):
        params["tool_type"] = args["tool_type"]
    if args.get("diameter_min") is not None:
        params["diameter_min"] = args["diameter_min"]
    if args.get("diameter_max") is not None:
        params["diameter_max"] = args["diameter_max"]
    if args.get("material"):
        params["material"] = args["material"]
    if args.get("supplier_id"):
        params["supplier_id"] = args["supplier_id"]

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{base_url}/tool-catalog/search", params=params)
        resp.raise_for_status()
        data = resp.json()

    return {
        "results": data.get("items", data) if isinstance(data, dict) else data,
        "total": data.get("total", len(data)) if isinstance(data, dict) else len(data),
        "query": query,
    }


_BUILTIN_HANDLERS: dict[str, Any] = {
    "drawing_analysis_mcp": {
        "name": "drawing_analysis_mcp",
        "_method": "builtin",
        "_handler": _handle_drawing_analysis_mcp,
    },
    "tool_search_mcp": {
        "name": "tool_search_mcp",
        "_method": "builtin",
        "_handler": _handle_tool_search_mcp,
    },
}


async def load_mcp_tools(
    server_configs: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    """Start all configured MCP servers and return (tools, handlers).

    tools    — list of OpenAI function-calling dicts ready for AgentSession
    handlers — dict mapping sanitised function name → async callable(args) -> dict
    """
    all_tools: list[dict] = list(_BUILTIN_TOOL_SCHEMAS)
    handlers: dict[str, Any] = dict(_BUILTIN_HANDLERS)

    for cfg in server_configs:
        transport = cfg.get("transport", "stdio")
        name = cfg.get("name", "mcp")

        if transport == "stdio":
            command = cfg.get("command", "")
            args = cfg.get("args", [])
            if not command:
                logger.warning("MCP stdio server '%s': no command specified, skipping", name)
                continue
            client: StdioMCPClient | HttpMCPClient = StdioMCPClient(name, command, args)
        elif transport in ("http", "streamable_http"):
            url = cfg.get("url", "")
            if not url:
                logger.warning("MCP http server '%s': no url specified, skipping", name)
                continue
            client = HttpMCPClient(name, url)
        else:
            logger.warning("MCP server '%s': unknown transport '%s', skipping", name, transport)
            continue

        ok = await _start_with_retry(client)
        if not ok:
            continue

        prefix = _sanitize(name)
        for tool in client.tools:
            schema = _mcp_tool_to_openai(tool, prefix)
            fn_name = schema["function"]["name"]
            all_tools.append(schema)

            raw_tool_name = tool.get("name", "")
            captured_client = client
            captured_raw = raw_tool_name

            async def _handler(args: dict, _c: Any = captured_client, _t: str = captured_raw) -> dict:
                return await _c.call_tool(_t, args)

            handlers[fn_name] = {
                "name": fn_name,
                "_url": "",
                "_method": "mcp",
                "_handler": _handler,
            }

        logger.info("MCP '%s': registered %d tools", name, len(client.tools))

    return all_tools, handlers
