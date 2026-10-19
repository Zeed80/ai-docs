"""The agent's own reply stays in the history the next turn reads.

Live case: the agent showed a letter and asked «Подтверждаю отправку?»; the
person answered «да», but the reply was not in the history, so the «да» had
nothing to refer to. Sending itself goes through the approval gate.
"""

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
