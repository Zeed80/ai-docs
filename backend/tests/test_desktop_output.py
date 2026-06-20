"""Phase 5 — desktop output by intent, not by keyword.

When the orchestrator routes a turn to the workspace, a structural result is
auto-published to the desktop even if the user's phrasing has no trigger word.
"""

import pytest
from unittest.mock import AsyncMock

from app.ai.agent_loop import AgentSession


def _session_with_user(text: str) -> AgentSession:
    s = AgentSession(send=AsyncMock())
    s.messages = [{"role": "user", "content": text}]
    s._publish_canvas = AsyncMock()
    s._send = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_workspace_expected_publishes_without_keyword():
    s = _session_with_user("разложи затраты по месяцам")  # no "таблица"/"список" marker
    s.set_workspace_expected(True)
    long_text = "Январь — 100; Февраль — 200; Март — 300. " * 8  # >200 chars, not markdown
    await s._deliver_final_content(long_text)
    s._publish_canvas.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_channel_no_publish():
    s = _session_with_user("спасибо за помощь")
    s.set_workspace_expected(False)
    await s._deliver_final_content("Пожалуйста! Рад помочь." * 10)
    s._publish_canvas.assert_not_awaited()


@pytest.mark.asyncio
async def test_markdown_table_always_publishes():
    s = _session_with_user("что-нибудь")
    s.set_workspace_expected(False)
    table = "| Поставщик | Сумма |\n|---|---|\n| Ромашка | 100 |\n| Берёзка | 200 |"
    await s._deliver_final_content(table)
    s._publish_canvas.assert_awaited_once()
