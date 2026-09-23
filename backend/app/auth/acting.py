"""Use the identity verified by the authentication boundary, never raw headers."""

from fastapi import Depends, Request

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo

AGENT_SERVICE_SUB = "agent-service"


async def get_effective_user(
    request: Request,
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    if user.sub != AGENT_SERVICE_SUB:
        return user
    # Impersonation via `X-Acting-User` is gone — the acting human now comes from
    # the verified execution context, not a header. The agent MARK stays on the
    # user itself: visibility rules (app.domain.email_access) read `via_agent`
    # and must not depend on whether a given endpoint remembered to pass a flag.
    return user.model_copy(update={"via_agent": True})
