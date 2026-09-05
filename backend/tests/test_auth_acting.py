"""get_effective_user (app.auth.acting) — agent-acts-for-human de-escalation.

No test file existed for this before Ф2 (AGENT_AUTONOMY_ROADMAP.md), even
though it's the dependency several endpoints rely on for "the agent calls
this on a human's behalf" — including app.api.computer_use, fixed in Ф2.A to
depend on this instead of get_current_user (which resolves the agent's own
X-API-Key call to sub="agent-service", not the human whose WorkOrder it is —
see that fix's comment). Filling the gap here since Ф2 now leans on it too.

get_effective_user opens its own DB session internally (_get_session_factory(),
not a passed-in one — unlike execute_claimed_step/process_work_learning
elsewhere in this codebase, it takes no session_factory parameter), so the
DB-touching tests here monkeypatch app.db.session._get_session_factory to a
real async_sessionmaker bound to test_engine (fresh session per call, not the
db_session fixture's own open transaction — reusing that object as the
factory's return value would have get_effective_user's own `async with`
close it out from under the fixture).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.acting import AGENT_SERVICE_SUB, get_effective_user
from app.auth.models import UserInfo, UserRole
from app.db.models import User


def _request(headers: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = MagicMock()
    req.headers.get = lambda key, default=None: (headers or {}).get(key.lower(), default)
    return req


def _agent_service_user() -> UserInfo:
    return UserInfo(
        sub=AGENT_SERVICE_SUB,
        email="agent@internal",
        name="AI Agent",
        preferred_username="agent",
        roles=[UserRole.admin],
        groups=["agents"],
    )


@pytest.fixture
def _patched_session_factory(test_engine, monkeypatch):
    """Make app.db.session._get_session_factory() return a real factory bound
    to test_engine, so get_effective_user's internal `_get_session_factory()()`
    hits the test database instead of whatever DATABASE_URL settings resolve
    to outside a test run."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.db.session._get_session_factory", lambda: factory)
    return factory


@pytest.mark.asyncio
async def test_human_caller_is_returned_unchanged():
    """The common case: a real human's own token, not the agent's key. No DB
    lookup should even be attempted — request.headers is never consulted."""
    human = UserInfo(
        sub="local:alice", email="a@example.com", name="Alice", preferred_username="alice"
    )
    request = MagicMock()
    result = await get_effective_user(request, human)
    assert result is human
    request.headers.get.assert_not_called()


@pytest.mark.asyncio
async def test_agent_service_with_no_acting_header_stays_the_bare_service_account():
    """A headless agent turn with nobody to act for — owns nothing, per the
    module docstring's "a headless agent turn -> the bare service account"."""
    agent = _agent_service_user()
    result = await get_effective_user(_request(), agent)
    assert result.sub == AGENT_SERVICE_SUB


@pytest.mark.asyncio
async def test_agent_service_with_acting_header_resolves_to_that_human(_patched_session_factory):
    factory = _patched_session_factory
    async with factory() as db:
        db.add(
            User(
                sub="local:bob",
                email="bob@example.com",
                name="Bob",
                preferred_username="bob",
                role="engineer",
                is_active=True,
            )
        )
        await db.commit()

    result = await get_effective_user(
        _request({"x-acting-user": "local:bob"}), _agent_service_user()
    )

    assert result.sub == "local:bob"
    assert result.roles == [UserRole.engineer]
    assert result.name == "Bob"


@pytest.mark.asyncio
async def test_agent_service_acting_for_unknown_user_stays_the_service_account(
    _patched_session_factory,
):
    """ "Unknown/inactive actor -> stay on the service account rather than
    inventing an identity" (the module's own stated contract)."""
    result = await get_effective_user(
        _request({"x-acting-user": "local:no-such-user"}), _agent_service_user()
    )
    assert result.sub == AGENT_SERVICE_SUB


@pytest.mark.asyncio
async def test_agent_service_acting_for_deactivated_user_stays_the_service_account(
    _patched_session_factory,
):
    factory = _patched_session_factory
    async with factory() as db:
        db.add(
            User(
                sub="local:carol",
                email="carol@example.com",
                name="Carol",
                preferred_username="carol",
                role="viewer",
                is_active=False,
            )
        )
        await db.commit()

    result = await get_effective_user(
        _request({"x-acting-user": "local:carol"}), _agent_service_user()
    )

    assert result.sub == AGENT_SERVICE_SUB


@pytest.mark.asyncio
async def test_acting_header_ignored_when_caller_is_not_the_agent_service():
    """X-Acting-User is only honoured for the agent-service key — any other
    caller can't impersonate anyone by sending it (module docstring)."""
    human = UserInfo(
        sub="local:alice", email="a@example.com", name="Alice", preferred_username="alice"
    )
    result = await get_effective_user(_request({"x-acting-user": "local:bob"}), human)
    assert result.sub == "local:alice"
