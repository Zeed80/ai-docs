"""Unified memory access layer for AgentSession.

Wraps the existing /api/memory endpoints so the agent loop doesn't
need to know the HTTP details.  All calls are async (httpx) and
fire-and-forget where appropriate.

Pattern adapted from hermes-agent/agent/memory_manager.py.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MEMORY_CONTEXT_TAG_OPEN = "<memory-context>"
_MEMORY_CONTEXT_TAG_CLOSE = "</memory-context>"

# Max chars for the evidence pack injected into the model context. Retrieval can
# inspect far more records server-side; this only bounds the final LLM payload.
_MAX_MEMORY_CHARS = 8000
_MEMORY_PAGE_SIZE = 20


class MemoryManager:
    """Thin async wrapper around the project's /api/memory HTTP endpoints.

    Usage in AgentSession::

        mgr = MemoryManager(base_url="http://localhost:8000")
        context_block = await mgr.prefetch(user_text)
        # inject context_block as a system-level user message before the LLM call

        asyncio.create_task(mgr.sync_turn(user_text, assistant_text))
    """

    def __init__(
        self,
        base_url: str,
        max_chars: int = _MAX_MEMORY_CHARS,
        timeout: float = 25.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_chars = max_chars
        self._timeout = timeout

    async def prefetch(self, query: str, session_id: str = "") -> str:
        """Search memory for context relevant to *query*.

        Calls ``POST /api/memory/search``.

        Returns a formatted ``<memory-context>`` block, or an empty string
        if nothing relevant was found or the call failed.
        """
        if not query.strip():
            return ""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                data = await self._read_memory_pages(client, query, session_id=session_id)
        except Exception as exc:
            logger.debug("memory prefetch failed (non-fatal): %s", exc)
            return ""

        hits: list[dict] = data
        if not hits:
            return ""

        lines: list[str] = []
        total_chars = 0
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            title = str(hit.get("title") or hit.get("kind") or "memory")
            summary = str(hit.get("summary") or "")[:800]
            source = str(hit.get("source") or "memory")
            line = f"- [{source}] {title}: {summary}".strip()
            if not summary and not title:
                continue
            if total_chars + len(line) > self._max_chars:
                break
            lines.append(line)
            total_chars += len(line)

        if not lines:
            return ""

        return self.build_context_block("\n".join(lines))

    async def _read_memory_pages(
        self,
        client: httpx.AsyncClient,
        query: str,
        *,
        session_id: str = "",
    ) -> list[dict]:
        body: dict[str, Any] = {
            "query": query[:500],
            "limit": _MEMORY_PAGE_SIZE,
            "retrieval_mode": "auto_hybrid",
            "need_full_coverage": False,
            "include_explain": False,
        }
        if session_id:
            body["session_id"] = session_id
        resp = await client.post(
            f"{self._base_url}/api/memory/search",
            json=body,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [h for h in (data.get("hits") or []) if isinstance(h, dict)]

    async def sync_turn(
        self,
        user_text: str,
        assistant_text: str,
        *,
        session_id: str = "",
    ) -> None:
        """Persist chat turn into long-term memory.

        The server stores this as episodic memory. Failures are non-fatal
        because chat delivery must not depend on memory indexing.
        """
        if not user_text.strip() and not assistant_text.strip():
            return
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(
                    f"{self._base_url}/api/memory/chat-turn",
                    json={
                        "user_text": user_text,
                        "assistant_text": assistant_text,
                        "session_id": session_id or None,
                        "scope": "project",
                    },
                )
        except Exception as exc:
            logger.debug("memory sync_turn failed (non-fatal): %s", exc)

    @staticmethod
    def build_context_block(raw: str) -> str:
        return f"{_MEMORY_CONTEXT_TAG_OPEN}\n{raw}\n{_MEMORY_CONTEXT_TAG_CLOSE}"
