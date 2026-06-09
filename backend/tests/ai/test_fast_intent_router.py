"""Tests for the deterministic fast-path intent router."""

from __future__ import annotations

import pytest

from app.ai.fast_intent_router import match_fast_intent


@pytest.mark.parametrize(
    "text,capability",
    [
        ("сколько счетов", "invoices"),
        ("сколько всего счетов?", "invoices"),
        ("сколько инвойсов в системе", "invoices"),
        ("сколько у нас поставщиков", "suppliers"),
        ("сколько контрагентов", "suppliers"),
        ("сколько документов загружено", "documents"),
        ("сколько аномалий", "anomalies"),
        ("сколько фрез на складе", "warehouse"),
        ("сколько резцов в наличии", "warehouse"),
    ],
)
def test_count_intents_match(text, capability):
    intent = match_fast_intent(text)
    assert intent is not None
    assert intent.capability == capability


@pytest.mark.parametrize(
    "text",
    [
        "покажи все счета таблицей",       # rich output → LLM
        "выведи счета по поставщикам",      # grouping → LLM
        "построй график по месяцам",        # chart → LLM
        "экспортируй счета в excel",         # export → LLM
        "объясни почему счёт отклонён",      # not a count
        "утверди счёт 42",                   # action
        "сколько стоит счёт 123",            # price, not count
        "сколько денег по счёту",            # amount, not count
        "на какую сумму счетов",             # amount, not count
        "остатки болтов на складе",          # no count marker → may want a table
        "",                                   # empty
    ],
)
def test_non_count_intents_defer(text):
    assert match_fast_intent(text) is None


def test_inventory_term_is_generic():
    """The search term must be extracted generically — no hardcoded categories."""
    for term_word, expected in [
        ("фрез", "фрез"),
        ("резцов", "резцов"),
        ("подшипников", "подшипников"),
    ]:
        intent = match_fast_intent(f"сколько {term_word} на складе")
        assert intent is not None
        assert intent.capability == "warehouse"
        assert intent.search_term == expected


def test_inventory_without_term():
    intent = match_fast_intent("сколько позиций на складе")
    assert intent is not None
    assert intent.capability == "warehouse"
    # "позиций" is filler → no concrete search term
    assert intent.search_term is None
