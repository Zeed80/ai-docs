"""Effective-user resolution for agent-initiated calls.

The agent authenticates as the ``agent-service`` account (admin, no owner of
anything) and states the human it acts for in ``X-Acting-User``
(app.ai.actor_context). Endpoints that scope data per user depend on
``get_effective_user`` instead of ``get_current_user`` so that:

  * a human request           → that human, unchanged;
  * an agent turn for a human → that human (de-escalation: the agent sees no
                                more than the person it answers);
  * a headless agent turn     → the bare service account, which owns nothing.

The header is deliberately honoured *only* for the internal service key. Any
other caller cannot impersonate anyone by sending it.
"""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import select

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo, UserRole

AGENT_SERVICE_SUB = "agent-service"


async def get_effective_user(
    request: Request,
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    if user.sub != AGENT_SERVICE_SUB:
        return user

    actor = (request.headers.get("x-acting-user") or "").strip()
    if not actor or actor == AGENT_SERVICE_SUB:
        return user

    from app.db.models import User
    from app.db.session import _get_session_factory

    async with _get_session_factory()() as db:
        row = (
            await db.execute(select(User).where(User.sub == actor, User.is_active == True))  # noqa: E712
        ).scalar_one_or_none()

    if row is None:
        # Unknown/inactive actor — stay on the service account rather than
        # inventing an identity.
        return user

    try:
        roles = [UserRole(row.role)]
    except ValueError:
        roles = [UserRole.viewer]
    return UserInfo(
        sub=row.sub,
        email=row.email,
        name=row.name,
        preferred_username=row.preferred_username,
        roles=roles,
        groups=list(user.groups or []),
        department_id=str(row.department_id) if row.department_id else None,
    )
