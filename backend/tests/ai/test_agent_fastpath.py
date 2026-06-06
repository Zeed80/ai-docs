"""A single Workspace-publish tool that returns a ready message ends the turn
without a second LLM round trip (the model would only paraphrase "table ready").
"""

from app.ai.agent_loop import AgentSession


def test_publish_message_is_terminal():
    msg = AgentSession._terminal_publish_reply(
        [("workspace", {"status": "published", "canvas_id": "agent:invoice-pivot",
                          "message": "Открыл таблицу: 5 групп"})]
    )
    assert msg == "Открыл таблицу: 5 групп"


def test_non_publish_or_multi_tool_not_terminal():
    # Not published → keep the normal flow (the model still needs to answer).
    assert AgentSession._terminal_publish_reply(
        [("invoices", {"status": "ok", "items": []})]
    ) is None
    # More than one tool → may need synthesis.
    assert AgentSession._terminal_publish_reply(
        [("workspace", {"status": "published", "canvas_id": "x", "message": "m"}),
         ("invoices", {"status": "ok"})]
    ) is None
    # Published but no message → can't short-circuit.
    assert AgentSession._terminal_publish_reply(
        [("workspace", {"status": "published", "canvas_id": "x"})]
    ) is None
