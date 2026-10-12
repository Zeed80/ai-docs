"""T7b: a guessed target is judged by code from this turn's reads (E52)."""

import json

from app.ai.agent_loop import guessed_target

STEEL = "11111111-1111-1111-1111-111111111111"
COPPER = "22222222-2222-2222-2222-222222222222"


def _turn(*reads, request="Обнови телефон поставщика ООО «Эвал-Никель»"):
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": request}]
    for index, (name, arguments, result) in enumerate(reads):
        call_id = f"c{index}"
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})
    return messages


def _envelope(data):
    return {"version": 1, "status": "succeeded", "data": data}


UPDATE = {"action": "update", "supplier_id": COPPER, "body": {"contact_phone": "+7 495 222-33-44"}}
LIST_ALL = (
    "suppliers",
    {"action": "list"},
    _envelope({"items": [{"id": STEEL}, {"id": COPPER}], "total": 2}),
)


def test_a_change_after_the_named_record_was_not_found_is_a_guess():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
        LIST_ALL,
    )
    assert guessed_target("suppliers", UPDATE, messages) == "requested_record_not_found"


def test_one_of_several_found_by_name_is_a_guess():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал"},
            _envelope({"results": [{"id": STEEL}, {"id": COPPER}], "total": 2}),
        ),
    )
    assert guessed_target("suppliers", UPDATE, messages) == "one_of_several"


def test_a_record_read_alone_is_confirmed():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Медь"},
            _envelope({"results": [{"id": COPPER}], "total": 1}),
        ),
    )
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_records_picked_by_a_filter_are_not_a_guess():
    """Bulk work on a filtered list (status, amount) is the user's criterion."""
    messages = _turn(
        (
            "suppliers",
            {"action": "list", "filters": {"status": "active"}},
            _envelope({"items": [{"id": STEEL}, {"id": COPPER}], "total": 2}),
        ),
        request="Отметь всех активных поставщиков",
    )
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_an_id_the_user_named_is_not_a_guess():
    messages = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
        request=f"Обнови телефон поставщика {COPPER}",
    )
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_a_read_from_an_earlier_turn_does_not_count():
    earlier = _turn(
        (
            "suppliers",
            {"action": "search", "query": "Эвал-Никель"},
            _envelope({"results": [], "total": 0}),
        ),
    )
    messages = earlier + [
        {"role": "assistant", "content": "Нет такого"},
        {"role": "user", "content": "Ок, меди"},
    ]
    assert guessed_target("suppliers", UPDATE, messages) is None


def test_creates_and_reads_are_never_judged():
    messages = _turn(
        ("suppliers", {"action": "search", "query": "x"}, _envelope({"results": [], "total": 0})),
    )
    assert guessed_target("suppliers", {"action": "search", "query": "y"}, messages) is None
    assert (
        guessed_target("analytics", {"action": "calendar_create_reminder", "body": {}}, messages)
        is None
    )
