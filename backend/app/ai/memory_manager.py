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
        timeout: float = 25.0,
        retrieval_mode: str = "hybrid",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._top_k = top_k
        self._max_chars = max_chars
        self._timeout = timeout
        self._retrieval_mode = retrieval_mode

    async def prefetch(self, query: str, session_id: str = "") -> str:
        """Search memory for context relevant to *query*.

        Calls ``POST /api/memory/search`` (same contract as :func:`agent_loop._load_memory_context`).

        Returns a formatted ``<memory-context>`` block, or an empty string
        if nothing relevant was found or the call failed.
        """
        if not query.strip():
            return ""
        try:
            body: dict[str, Any] = {
                "query": query[:500],
                "limit": self._top_k,
                "retrieval_mode": self._retrieval_mode,
                "include_explain": False,
            }
            if session_id:
                body["session_id"] = session_id
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/api/memory/search",
                    json=body,
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
        except Exception as exc:
            logger.debug("memory prefetch failed (non-fatal): %s", exc)
            return ""

        hits: list[dict] = data.get("hits") or []
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

    async def sync_turn(self, user_text: str, assistant_text: str) -> None:
        """Persist chat turn into long-term memory.

        Episodic chat indexing (dedicated HTTP route) is not implemented yet;
        document/graph memory is updated through ingest and approval pipelines.
        """
        if not user_text.strip() and not assistant_text.strip():
            return
        # Reserved for future POST /api/memory/chat-turn or embedding queue.
        return

    @staticmethod
    def build_context_block(raw: str) -> str:
        return f"{_MEMORY_CONTEXT_TAG_OPEN}\n{raw}\n{_MEMORY_CONTEXT_TAG_CLOSE}"
