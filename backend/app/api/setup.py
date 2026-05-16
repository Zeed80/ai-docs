"""Setup API — one-time admin bootstrap endpoint.

Available only when SETUP_TOKEN is set in env and has not been used yet.
After the first successful call the token is invalidated (flag in Redis).
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_db
from fastapi import Depends

router = APIRouter()
logger = structlog.get_logger()

_BOOTSTRAP_KEY = "setup:admin_bootstrapped"


class BootstrapRequest(BaseModel):
    token: str
    email: str


@router.post("/bootstrap-admin")
async def bootstrap_admin(
    payload: BootstrapRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Promote a user to admin using the one-time SETUP_TOKEN.

    The token is read from the SETUP_TOKEN environment variable and is
    invalidated after first successful use. Useful for CI/CD or Docker setups
    where INITIAL_ADMIN_EMAIL cannot be used.
    """
    if not settings.setup_token:
        raise HTTPException(status_code=404, detail="Setup endpoint not available")

    # Check one-time use flag in Redis
    _redis = None
    try:
        from app.utils.redis_client import get_async_redis
        _redis = get_async_redis()
        if await _redis.get(_BOOTSTRAP_KEY):
            raise HTTPException(status_code=403, detail="Setup token already used")
    except HTTPException:
        raise
    except Exception:
        # Redis unavailable — allow but warn
        logger.warning("redis_unavailable_for_setup_check")

    if payload.token != settings.setup_token:
        raise HTTPException(status_code=403, detail="Invalid setup token")

    from app.db.models import User
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail=f"User with email {payload.email!r} not found. Log in first.")

    user.role = "admin"
    await db.commit()

    # Invalidate token
    try:
        if _redis is not None:
            await _redis.set(_BOOTSTRAP_KEY, "1")
    except Exception:
        pass

    logger.info("admin_bootstrapped", email=payload.email, sub=user.sub)
    return {"status": "admin promoted", "sub": user.sub, "email": user.email}
