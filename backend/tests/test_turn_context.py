"""A short follow-up is read in the context of the conversation.

The router and the verifier both saw only the last message: «2» (the user
picking an option) went as smalltalk, and «И?» or «Выбери сама путь» failed
verification after correct work.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.ai.turn_router import build_router_user
from app.db.models import ChatMessage, ChatSession
from app.domain.agent_intake import _preceding_dialogue


def test_router_sees_the_preceding_exchange():
    text = build_router_user(
        "2",
        has_open_spec_table=False,
        history_summary="Ассистент: Варианты: 1) повторить позже 2) искать зеркало",
    )
    assert "искать зеркало" in text
    assert "продолжает задачу" in text
    assert text.endswith("Сообщение пользователя:\n2")


def test_router_without_history_is_unchanged():
    assert "Недавний контекст" not in build_router_user("Привет", has_open_spec_table=False)


@pytest.mark.asyncio
async def test_intake_context_holds_the_last_exchanges(db_session):
    session = ChatSession(user_key="dev-user", title="t")
    db_session.add(session)
    await db_session.flush()
    start = datetime(2026, 10, 16, tzinfo=UTC)
    for minute, (role, content) in enumerate(
        [
            ("user", "старое, не нужно"),
            ("user", "Найди каталог Хоффманна"),
            ("assistant", "Есть два пути: 1) сайт 2) зеркало. Какой?"),
            ("tool", "служебное"),
            ("user", "Выбери сама путь"),
            ("assistant", "Пошла через сайт."),
        ]
    ):
        db_session.add(
            ChatMessage(
                session_id=session.id,
                role=role,
                content=content,
                created_at=start + timedelta(minutes=minute),
            )
        )
        await db_session.flush()

    context = await _preceding_dialogue(db_session, session.id)

    assert context.startswith("Предыдущий разговор:")
    assert "старое" not in context and "служебное" not in context
    assert context.index("Найди каталог") < context.index("Пошла через сайт")


@pytest.mark.asyncio
async def test_first_message_has_no_context(db_session):
    session = ChatSession(user_key="dev-user", title="t")
    db_session.add(session)
    await db_session.flush()
    assert await _preceding_dialogue(db_session, session.id) == ""
