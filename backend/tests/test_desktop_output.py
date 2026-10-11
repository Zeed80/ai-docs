"""Phase 5 — desktop output by intent, not by keyword.

When the orchestrator routes a turn to the workspace, a structural result is
auto-published to the desktop even if the user's phrasing has no trigger word.
"""

from unittest.mock import AsyncMock

import pytest

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


@pytest.mark.asyncio
async def test_orchestrator_chat_decision_beats_the_keyword_gate():
    """'без таблицы' contains 'таблиц'; the router's chat decision must win."""
    s = _session_with_user("Назови в чате трёх поставщиков, без таблицы")
    s.set_workspace_expected(False)
    await s._deliver_final_content("Больше всего счетов у: Ромашка, Берёзка, Ёлочка. " * 6)
    s._publish_canvas.assert_not_awaited()


@pytest.mark.asyncio
async def test_keyword_fallback_only_without_a_decision():
    s = _session_with_user("выведи таблицу затрат")
    assert s._workspace_expected is None
    await s._deliver_final_content("Январь — 100; Февраль — 200; Март — 300. " * 8)
    s._publish_canvas.assert_awaited_once()


_PUBLISHED_V1 = {
    "version": 1,
    "status": "succeeded",
    "data": {
        "status": "published",
        "canvas_id": "agent:spec-table",
        "total": 4,
        "shown": 4,
        "message": "Опубликовал таблицу «Письма по отправителям»: 4 строк.",
    },
}


def test_publish_fast_path_reads_the_v1_envelope():
    """status "published" lives under data (live 2026-10-07)."""
    reply = AgentSession._terminal_publish_reply([("workspace", _PUBLISHED_V1)])
    assert reply == "Опубликовал таблицу «Письма по отправителям»: 4 строк."
    failed = {**_PUBLISHED_V1, "status": "failed"}
    assert AgentSession._terminal_publish_reply([("workspace", failed)]) is None


@pytest.mark.asyncio
async def test_after_a_publish_the_model_answers_the_whole_request(monkeypatch):
    """The publish message alone lost "и скажи, что с этим делать" and the
    verifier blocked the turn (live 2026-10-11): one tool-less call answers."""
    import app.ai.agent_loop as loop

    seen = {}

    async def _fake(messages, tools, system, config, on_token, **_kw):
        seen["tools"], seen["last"] = tools, messages[-1]["content"]
        await on_token("Таблица на столе. Запросите email у трёх поставщиков.")
        return {}

    monkeypatch.setattr(loop, "_call_provider_streaming", _fake)
    s = _session_with_user("перечисли поставщиков без email и скажи, что делать")
    s._effective_system = lambda: ""
    s._record_assistant_reply = lambda _text: None
    s._remember_latest_turn = lambda _text: None
    reply = "Опубликовал таблицу «Поставщики без email»: 3 строк."
    await s._force_final_answer(
        instruction=f"Результат уже на рабочем столе: {reply}", fallback=reply
    )
    assert seen["tools"] == [] and reply in seen["last"]
    s._send.assert_awaited_once_with(
        {"type": "text", "content": "Таблица на столе. Запросите email у трёх поставщиков."}
    )


@pytest.mark.asyncio
async def test_a_silent_model_after_a_publish_still_gets_the_publish_message(monkeypatch):
    import app.ai.agent_loop as loop

    async def _silent(*_a, **_kw):
        return {}

    monkeypatch.setattr(loop, "_call_provider_streaming", _silent)
    s = _session_with_user("покажи поставщиков без email")
    s._effective_system = lambda: ""
    s._record_assistant_reply = lambda _text: None
    s._remember_latest_turn = lambda _text: None
    reply = "Опубликовал таблицу «Поставщики без email»: 3 строк."
    await s._force_final_answer(instruction="…", fallback=reply)
    s._send.assert_awaited_once_with({"type": "text", "content": reply})


@pytest.mark.asyncio
async def test_a_retold_table_does_not_replace_the_published_one():
    """The model's markdown table replaced the real SQL table on the desktop
    with a placeholder row "(данные по 4 адресам)" (live 2026-10-07)."""
    import json

    s = _session_with_user("сколько писем от каждого отправителя")
    s.messages.append({"role": "tool", "content": json.dumps(_PUBLISHED_V1, ensure_ascii=False)})
    table = "| Отправитель | Количество |\n|---|---|\n| (данные по 4 адресам) | всего |"
    await s._deliver_final_content(table)
    s._publish_canvas.assert_not_awaited()
    s._send.assert_awaited_once_with({"type": "text", "content": table})


@pytest.mark.asyncio
async def test_a_published_table_of_an_earlier_turn_does_not_count():
    import json

    s = _session_with_user("покажи письма")
    s.messages.append({"role": "tool", "content": json.dumps(_PUBLISHED_V1, ensure_ascii=False)})
    s.messages.append({"role": "user", "content": "а теперь таблицу затрат"})
    await s._deliver_final_content("| Месяц | Сумма |\n|---|---|\n| Январь | 100 |")
    s._publish_canvas.assert_awaited_once()


def test_sql_table_preview_carries_the_rows_by_label():
    from app.api.workspace import _table_preview

    block = {
        "columns": [{"key": "a", "label": "Отправитель"}, {"key": "b", "label": "Писем"}],
        "rows": [{"a": "x@y", "b": 12}, {"a": "z" * 500, "b": 1}],
    }
    preview = _table_preview(block)
    assert preview[0] == {"Отправитель": "x@y", "Писем": 12}
    assert len(preview[1]["Отправитель"]) == 201


@pytest.mark.asyncio
async def test_sql_table_answer_is_a_sentence_and_the_query_is_kept_whole(monkeypatch):
    """The publish fast path shows the tool message as the answer; it ended
    with a SQL snippet cut mid-word (live 2026-10-07)."""
    from app.api import workspace

    long_sql = (
        "SELECT p.name, COUNT(i.id) FROM invoices i JOIN parties p ON p.id = i.supplier_id " * 3
    )

    async def build(**_kwargs):
        return {
            "status": "ok",
            "sql": long_sql,
            "data": {"type": "table", "title": "Счета", "columns": [], "rows": [{"a": 1}]},
        }

    monkeypatch.setattr("app.ai.table_sql_pipeline.build_table_from_task", build)
    monkeypatch.setattr(workspace, "upsert_workspace_block", lambda cid, block: block)

    async def publish(_event):
        return None

    monkeypatch.setattr(workspace.chat_bus, "publish", publish)
    response = await workspace._publish_sql_table(
        workspace.WorkspaceSqlTableRequest(task="счета", canvas_id="agent:spec-table")
    )
    assert response.message == "Опубликовал таблицу «Счета»: 1 строк."
    assert response.sql == long_sql


@pytest.mark.asyncio
async def test_a_table_reply_keeps_the_words_about_what_was_done():
    """E49 live: "marked 2 invoices" + a table became only "opened a table"."""
    s = _session_with_user("пометь счета и покажи их")
    s.set_workspace_expected(False)
    text = "Пометила 2 счёта «запросить акт».\n\n| Счёт | Сумма |\n|---|---|\n| A | 1 |\n| B | 2 |"
    reply = await s._deliver_final_content(text)
    s._publish_canvas.assert_awaited_once()
    assert reply.startswith("Пометила 2 счёта")
    assert "| A |" not in reply


def test_publish_fast_path_is_skipped_when_the_turn_changed_records():
    import json

    from app.ai.agent_loop import performed_changes

    messages = [
        {"role": "user", "content": "пометь и покажи"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "u1", "function": {"name": "invoices", "arguments": {"action": "update"}}}
            ],
        },
        {"role": "tool", "tool_call_id": "u1", "content": json.dumps({"status": "succeeded"})},
    ]
    assert performed_changes(messages) == ["invoices.update"]
    # A later turn starts clean: earlier changes are not this turn's.
    assert performed_changes([*messages, {"role": "user", "content": "спасибо"}]) == []
