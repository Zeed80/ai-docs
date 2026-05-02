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


async def load_mcp_tools(
    server_configs: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    """Start all configured MCP servers and return (tools, handlers).

    tools    — list of OpenAI function-calling dicts ready for AgentSession
    handlers — dict mapping sanitised function name → async callable(args) -> dict
    """
    all_tools: list[dict] = []
    handlers: dict[str, Any] = {}

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
