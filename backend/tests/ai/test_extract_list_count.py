"""Counts are read through the ToolResult v1 envelope, never guessed as 0."""

from app.ai.agent_loop import extract_list_count


def _envelope(data, status="succeeded"):
    return {"version": 1, "status": status, "data": data, "evidence": {}, "checkpoint": None}


def test_total_inside_a_succeeded_envelope():
    assert extract_list_count(_envelope({"items": [{"id": 1}], "total": 39})) == 39


def test_items_length_inside_an_envelope():
    assert extract_list_count(_envelope({"items": [1, 2, 3]})) == 3


def test_failed_envelope_is_unknown_not_zero():
    assert extract_list_count(_envelope({"total": 5}, status="failed")) is None


def test_unreadable_payload_is_unknown_not_zero():
    assert extract_list_count({"message": "ok"}) is None
    assert extract_list_count("text") is None
    assert extract_list_count({"total": True}) is None


def test_legacy_shapes_still_count():
    assert extract_list_count({"total": 0}) == 0
    assert extract_list_count({"items": []}) == 0
    assert extract_list_count([1, 2]) == 2
