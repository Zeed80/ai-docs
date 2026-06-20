"""Tool-call ↔ result linking via tool_call_id (Phase 2 refactor).

Regression guard: tool results must be linked to their originating call by id,
not by positional FIFO order, otherwise parallel/multi-step turns mis-attribute
results and the model "loses the thread".
"""

from app.ai.agent_loop import _convert_messages_to_anthropic, _normalize_openai_messages


def test_anthropic_links_results_by_id_when_out_of_order():
    # Assistant issues two parallel tool calls; results arrive in REVERSE order.
    messages = [
        {"role": "user", "content": "посчитай"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_A", "function": {"name": "invoices", "arguments": {"action": "list"}}},
                {"id": "call_B", "function": {"name": "documents", "arguments": {"action": "list"}}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_B", "content": '{"docs": 7}'},
        {"role": "tool", "tool_call_id": "call_A", "content": '{"invoices": 3}'},
    ]

    _system, converted = _convert_messages_to_anthropic(messages, "sys")

    # Last block is a user turn holding both tool_result blocks.
    tool_results = converted[-1]["content"]
    by_id = {b["tool_use_id"]: b["content"] for b in tool_results}
    assert by_id["call_B"] == '{"docs": 7}'
    assert by_id["call_A"] == '{"invoices": 3}'


def test_anthropic_falls_back_to_fifo_without_ids():
    # Legacy messages with no tool_call_id still convert (positional fallback).
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "x", "function": {"name": "invoices", "arguments": {}}}],
        },
        {"role": "tool", "content": '{"ok": true}'},
    ]
    _system, converted = _convert_messages_to_anthropic(messages, "sys")
    assert converted[-1]["content"][0]["tool_use_id"] == "x"


def test_normalize_openai_preserves_tool_call_id_on_tool_messages():
    messages = [
        {"role": "tool", "tool_call_id": "call_Z", "content": "{}"},
    ]
    out = _normalize_openai_messages(messages)
    assert out[0]["tool_call_id"] == "call_Z"
