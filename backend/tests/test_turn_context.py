"""A short follow-up is read in the context of the conversation.

The verifier saw only the last message: «И?» or «Выбери сама путь» failed
verification after correct work.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.db.models import ChatMessage, ChatSession
from app.domain.agent_intake import _preceding_dialogue


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
