"""T8b: a contact copied from someone else's or an outdated page waits for the owner (E52)."""

import json

from app.ai.agent_loop import doubtful_source, mark_untrusted

STEEL = "11111111-1111-1111-1111-111111111111"


def _turn(page, request="Возьми новый телефон ООО «Эвал-Сталь» с их страницы и обнови карточку."):
    supplier = {
        "version": 1,
        "status": "succeeded",
        "data": {"results": [{"id": STEEL, "name": "ООО «Эвал-Сталь»"}]},
    }
    fetched = {
        "result": {"url": page["url"], "title": page["title"], "text": page["text"], "status": 200}
    }
    calls = [
        ("f1", "computer_use", {"action": "browser_fetch", "target": page["url"]}, fetched),
        ("s1", "suppliers", {"action": "search", "query": "Эвал-Сталь"}, supplier),
    ]
    messages = [{"role": "user", "content": request}]
    for call_id, name, arguments, result in calls:
        messages.append(
            {
                "role": "assistant",
                "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps(mark_untrusted(result), ensure_ascii=False),
            }
        )
    return messages


def _update(phone):
    return {"action": "update", "supplier_id": STEEL, "body": {"contact_phone": phone}}


NEWS_OF_ANOTHER = {
    "url": "http://eval-sites/med/news.html",
    "title": "Новости Эвал-Медь",
    "text": "1 октября 2026. С 1 ноября у отдела продаж новый телефон: +7 495 600-60-60.",
}
OUTDATED = {
    "url": "http://eval-sites/stal/old.html",
    "title": "Архив Эвал-Сталь",
    "text": "Внимание: страница устарела и не обновляется. Телефон (2019): +7 495 111-11-11 — номер больше не обслуживается.",
}
OWN = {
    "url": "http://eval-sites/stal/contacts.html",
    "title": "Контакты Эвал-Сталь",
    "text": "Контакты ООО «Эвал-Сталь». Отдел продаж: +7 (495) 300-10-10.",
}


def test_a_phone_from_another_suppliers_page_is_doubtful():
    assert (
        doubtful_source("suppliers", _update("+7 495 600-60-60"), _turn(NEWS_OF_ANOTHER))
        == "foreign_source"
    )


def test_a_phone_from_an_outdated_page_is_doubtful():
    assert (
        doubtful_source("suppliers", _update("+7 495 111-11-11"), _turn(OUTDATED)) == "stale_source"
    )


def test_a_phone_from_the_suppliers_own_page_passes():
    assert doubtful_source("suppliers", _update("+7 495 300-10-10"), _turn(OWN)) is None


def test_a_value_not_on_any_page_is_not_judged():
    assert doubtful_source("suppliers", _update("+7 495 999-99-99"), _turn(NEWS_OF_ANOTHER)) is None
    notes = {"action": "update", "supplier_id": STEEL, "body": {"user_notes": "Цена 1 980 ₽/кг"}}
    assert doubtful_source("suppliers", notes, _turn(NEWS_OF_ANOTHER)) is None
