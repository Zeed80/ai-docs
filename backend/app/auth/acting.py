"""Use the identity verified by the authentication boundary, never raw headers."""

from fastapi import Depends, Request

from app.auth.jwt import get_current_user
from app.auth.models import UserInfo

AGENT_SERVICE_SUB = "agent-service"


async def get_effective_user(
    request: Request,
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    return user
