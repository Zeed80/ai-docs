"""«Да» на показанный черновик — это и есть разрешение.

Живой случай: агент показал письмо и спросил «Подтверждаю отправку?», человек
ответил «да» и получил ещё шесть запросов разрешения на то же самое письмо.

Здесь проверяется в первую очередь НЕ то, что согласие срабатывает, а то, что
оно не срабатывает лишний раз: это гейт внешнего действия, и цена ошибки —
письмо, ушедшее без ведома человека.
"""

import json

from app.ai.agent_loop import AgentSession


def _session(messages):
    s = AgentSession.__new__(AgentSession)
    s.messages = messages
    return s


PROPOSAL = (
    "Готово, черновик письма создан. Вот что будет отправлено:\n\n"
    "**Кому:** zeed@yandex.ru\n**Тема:** Запрос каталога концевых фрез\n\n"
    "> Добрый день!\n\nПодтверждаю отправку? (да/нет)"
)
SEND = {"action": "send", "draft_id": "fe1aa152-0000-4000-8000-000000000001"}


def test_yes_after_a_shown_draft_is_the_approval():
    s = _session(
        [
            {"role": "user", "content": "Отправь zeed@yandex.ru письмо"},
            {"role": "assistant", "content": PROPOSAL},
            {"role": "user", "content": "да"},
        ]
    )
    assert s._confirms_pending_send("email", SEND) is True


def test_other_affirmatives_work_too():
    for word in ("Да", "ок", "давай", "подтверждаю", "отправляй", "yes", "confirm"):
        s = _session(
            [
                {"role": "assistant", "content": PROPOSAL},
                {"role": "user", "content": word},
            ]
        )
        assert s._confirms_pending_send("email", SEND) is True, word


def test_a_yes_without_a_shown_draft_is_not_an_approval():
    """Иначе «да» из обсуждения чего угодно отправляло бы письмо."""
    s = _session(
        [
            {"role": "assistant", "content": "Нашла 12 счетов. Показать таблицу?"},
            {"role": "user", "content": "да"},
        ]
    )
    assert s._confirms_pending_send("email", SEND) is False


def test_negation_inside_a_short_answer_is_a_refusal():
    for text in ("да, но не отправляй", "нет", "пока нет", "стоп", "подожди"):
        s = _session(
            [
                {"role": "assistant", "content": PROPOSAL},
                {"role": "user", "content": text},
            ]
        )
        assert s._confirms_pending_send("email", SEND) is False, text


def test_a_long_message_is_not_a_bare_confirmation():
    """Развёрнутая реплика — это новая задача, а не ответ «да»."""
    s = _session(
        [
            {"role": "assistant", "content": PROPOSAL},
            {
                "role": "user",
                "content": (
                    "да, и заодно добавь в письмо просьбу прислать сроки поставки "
                    "и напиши второму поставщику тоже"
                ),
            },
        ]
    )
    assert s._confirms_pending_send("email", SEND) is False


def test_recipient_must_match_what_was_shown():
    """Подтверждали письмо одному адресату — уйти оно должно ему же."""
    s = _session(
        [
            {"role": "assistant", "content": PROPOSAL},
            {"role": "user", "content": "да"},
        ]
    )
    other = {
        "action": "send",
        "body": json.dumps({"to_addresses": ["someone-else@example.com"], "subject": "Тема"}),
    }
    assert s._confirms_pending_send("email", other) is False

    same = {
        "action": "send",
        "body": json.dumps(
            {"to_addresses": ["zeed@yandex.ru"], "subject": "Запрос каталога концевых фрез"}
        ),
    }
    assert s._confirms_pending_send("email", same) is True


def test_only_the_email_send_gate_is_covered():
    """Согласие на письмо не открывает другие внешние действия."""
    s = _session(
        [
            {"role": "assistant", "content": PROPOSAL},
            {"role": "user", "content": "да"},
        ]
    )
    assert s._confirms_pending_send("invoices", {"action": "approve"}) is False
    assert s._confirms_pending_send("email", {"action": "delete"}) is False


def test_confirmation_belongs_to_the_proposal_that_preceded_it():
    """Предложение ищется ДО ответа человека: подтвердить можно только уже
    показанное, а не то, что агент напишет следом."""
    s = _session(
        [
            {"role": "assistant", "content": PROPOSAL},
            {"role": "user", "content": "да"},
            {"role": "assistant", "content": "Уточните, пожалуйста, адрес."},
        ]
    )
    assert s._confirms_pending_send("email", SEND) is True


def test_the_agents_own_reply_lands_in_history():
    """Без этого «да» не к чему привязать.

    Ответ исполнителя в историю не попадал — в неё уходило только сообщение с
    вызовами инструментов. Агент не помнил, что сам сказал человеку минуту
    назад, а именно на это человек и отвечает.
    """
    s = AgentSession.__new__(AgentSession)
    s.messages = [{"role": "user", "content": "Отправь письмо"}]
    s._trim_history = lambda: None

    s._record_assistant_reply(PROPOSAL)
    assert s.messages[-1] == {"role": "assistant", "content": PROPOSAL}

    # Повтор того же текста не дублируется, пустой ответ не пишется.
    s._record_assistant_reply(PROPOSAL)
    s._record_assistant_reply("   ")
    assert len(s.messages) == 2


def test_end_to_end_shape_of_the_live_case():
    """Ровно та последовательность, что была на стенде."""
    s = AgentSession.__new__(AgentSession)
    s.messages = [{"role": "user", "content": "Отправь zeed@yandex.ru письмо о каталоге"}]
    s._trim_history = lambda: None

    s._record_assistant_reply(PROPOSAL)
    s.messages.append({"role": "user", "content": "да"})

    assert s._confirms_pending_send("email", SEND) is True
