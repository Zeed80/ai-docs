"""Unified memory access layer for AgentSession.

Wraps the existing /api/memory endpoints so the agent loop doesn't
need to know the HTTP details.  All calls are async (httpx) and
fire-and-forget where appropriate.

Pattern adapted from hermes-agent/agent/memory_manager.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MEMORY_CONTEXT_TAG_OPEN = "<memory-context>"
_MEMORY_CONTEXT_TAG_CLOSE = "</memory-context>"

# Max chars for a single prefetched memory block injected into context.
_MAX_MEMORY_CHARS = 4000


class MemoryManager:
    """Thin async wrapper around the project's /api/memory HTTP endpoints.

    Usage in AgentSession::

        mgr = MemoryManager(base_url="http://localhost:8000", top_k=8)
        context_block = await mgr.prefetch(user_text)
        # inject context_block as a system-level user message before the LLM call

        asyncio.create_task(mgr.sync_turn(user_text, assistant_text))
    """

    def __init__(
        self,
        base_url: str,
        top_k: int = 8,
        max_chars: int = _MAX_MEMORY_CHARS,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._top_k = top_k
        self._max_chars = max_chars
        self._timeout = timeout

    async def prefetch(self, query: str, session_id: str = "") -> str:
        """Search memory for context relevant to *query*.

        Returns a formatted ``<memory-context>`` block, or an empty string
        if nothing relevant was found or the call failed.
        """
        if not query.strip():
            return ""
        try:
            params: dict[str, Any] = {"q": query[:500], "top_k": self._top_k}
            if session_id:
                params["session_id"] = session_id
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/api/memory/search",
                    params=params,
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
        except Exception as exc:
            logger.debug("memory prefetch failed (non-fatal): %s", exc)
            return ""

        items: list[dict] = data.get("results") or data.get("items") or []
        if not items:
            return ""

        lines: list[str] = []
        total_chars = 0
        for item in items:
            text = item.get("content") or item.get("text") or ""
            if not text:
                continue
            chunk = text[:800]
            if total_chars + len(chunk) > self._max_chars:
                break
            lines.append(f"- {chunk}")
            total_chars += len(chunk)

        if not lines:
            return ""

        return self.build_context_block("\n".join(lines))

    async def sync_turn(self, user_text: str, assistant_text: str) -> None:
        """Index the completed turn into memory (fire-and-forget)."""
        if not user_text.strip() and not assistant_text.strip():
            return
        try:
            payload = {
                "user_message": user_text[:2000],
                "assistant_message": assistant_text[:2000],
            }
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(
                    f"{self._base_url}/api/memory/index",
                    json=payload,
                )
        except Exception as exc:
            logger.debug("memory sync_turn failed (non-fatal): %s", exc)

    @staticmethod
    def build_context_block(raw: str) -> str:
        return f"{_MEMORY_CONTEXT_TAG_OPEN}\n{raw}\n{_MEMORY_CONTEXT_TAG_CLOSE}"
