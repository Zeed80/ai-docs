"""Unit tests for frez (cutter) inventory fast-path helpers."""

from app.ai.agent_loop import (
    _frez_followup_intent_from_prior_user_message,
    _frez_inventory_intent,
    _is_short_scope_followup,
    _mentions_frez_intent,
)


def test_mentions_frez() -> None:
    assert _mentions_frez_intent("сколько всего фрез")
    assert _mentions_frez_intent("фрезы на складе")
    assert not _mentions_frez_intent("сколько токарных резцов")


def test_frez_inventory_intent() -> None:
    assert _frez_inventory_intent("Сколько всего фрез?") == "count"
    assert _frez_inventory_intent("Перечисли все фрезы") == "list"
    assert _frez_inventory_intent("Покажи остатки фрез") == "list"
    assert _frez_inventory_intent("Какие фрезы на складе?") == "list"


def test_short_followup() -> None:
    assert _is_short_scope_followup("все")
    assert _is_short_scope_followup("Всё.")
    assert not _is_short_scope_followup("все фрезы")


def test_followup_inherits_intent() -> None:
    assert _frez_followup_intent_from_prior_user_message("сколько фрез на складе?") == "count"
    assert _frez_followup_intent_from_prior_user_message("перечисли фрезы") == "list"
