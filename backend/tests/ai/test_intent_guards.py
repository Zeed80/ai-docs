"""Intent guards used by the proactive fast-path: action vs view, high-complexity."""
from __future__ import annotations

import pytest

from app.ai.model_tier import has_action_intent, has_high_complexity_signal


@pytest.mark.parametrize("text", [
    "Проверь арифметику последнего счёта от поставщика Хоффманн",
    "Утверди счёт 42",
    "Отправь письмо поставщику",
    "Пересчитай НДС в счёте",
    "validate invoice 10",
])
def test_action_intent_detected(text):
    assert has_action_intent(text)


@pytest.mark.parametrize("text", [
    "Покажи таблицу счетов",
    "Найди счета от поставщика Хоффманн",
    "Расскажи о товарах в счетах — какие популярнее",
    "Сколько счетов ожидают утверждения?",
])
def test_view_requests_not_action(text):
    assert not has_action_intent(text)


def test_high_complexity_vs_stacked_medium():
    # HIGH verb → needs worker
    assert has_high_complexity_signal("Сравни цены поставщиков")
    assert has_high_complexity_signal("Проанализируй динамику закупок")
    # stacked MEDIUM analytical words are NOT high-complexity (stay proactive)
    assert not has_high_complexity_signal("какие товары популярнее всего")
    assert not has_high_complexity_signal("покажи таблицу счетов")
