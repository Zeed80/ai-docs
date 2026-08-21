"""Vector calls must fail fast, not hold a chat turn hostage.

Live finding (2026-08-21): Ollama's /api/embed stopped responding on the
deployment; each embedding candidate then burned the provider's 180s
conversational timeout, and the fallback chain multiplied that — a finished
chat turn hung for ~19 minutes with the GPU idle.
"""

from __future__ import annotations

import asyncio

import pytest

from app.ai import router as router_module
from app.ai.schemas import AITask


def test_vector_tasks_declare_a_short_deadline():
    for task in (AITask.EMBEDDING, AITask.RERANKING):
        assert router_module._FAST_TASK_DISPATCH_TIMEOUT[task] <= 30
        assert router_module._FAST_TASK_CHAIN_BUDGET[task] <= 60


def test_conversational_tasks_keep_the_provider_timeout():
    """A reasoning model legitimately takes minutes — it must NOT be clamped."""
    assert AITask.ENGINEERING_REASONING not in router_module._FAST_TASK_DISPATCH_TIMEOUT
    assert AITask.INVOICE_OCR not in router_module._FAST_TASK_DISPATCH_TIMEOUT


@pytest.mark.asyncio
async def test_wait_for_cancels_a_hung_dispatch():
    """The mechanism itself: a dispatch that never returns is cut at the deadline."""

    async def never_returns():
        await asyncio.sleep(3600)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(never_returns(), timeout=0.05)
