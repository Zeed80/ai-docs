"""The deterministic count fast-path answers only a plain count (live 2026-10-13)."""

from app.ai.route_table import match_fast_intent


def test_a_plain_count_is_answered_fast():
    intent = match_fast_intent("Сколько поставщиков в справочнике?")
    assert intent is not None and intent.capability == "suppliers"


def test_a_count_with_a_condition_or_a_second_question_goes_to_the_model():
    """ "…и у скольких не указан телефон?" was answered "Всего поставщиков: 35"."""
    assert (
        match_fast_intent("Сколько поставщиков в справочнике и у скольких не указан телефон?")
        is None
    )
    assert match_fast_intent("Сколько поставщиков без телефона?") is None
    assert match_fast_intent("Сколько счетов и сколько поставщиков?") is None
