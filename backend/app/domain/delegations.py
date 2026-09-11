"""Explicit, owner-scoped standing authority; never inferred from conversation."""

import json
from datetime import UTC, datetime

from sqlalchemy import select

from app.db.agent_runtime_models import DelegationGrant


def normalized_arguments(arguments: dict) -> dict:
    body = {k: v for k, v in arguments.items() if k not in {"action", "reason"}}
    for key in ("filters", "body"):
        nested = body.get(key)
        if isinstance(nested, dict):
            body.pop(key)
            body.update(nested)
    return body


def arguments_match(constraints: dict, arguments: dict) -> bool:
    """Exact typed values only. No patterns, expressions or executable predicates."""
    body = normalized_arguments(arguments)
    return bool(constraints) and all(
        key in body and json.dumps(body[key], sort_keys=True) == json.dumps(value, sort_keys=True)
        for key, value in constraints.items()
    )


async def matching_delegation(
    owner: str,
    action: str,
    arguments: dict,
    *,
    consume: bool = False,
):
    from app.audit.service import log_action
    from app.db.session import _get_session_factory

    if not owner or owner in {"agent-service", "anonymous"}:
        return None
    now = datetime.now(UTC)
    async with _get_session_factory()() as db:
        query = (
            select(DelegationGrant)
            .where(
                DelegationGrant.owner_key == owner,
                DelegationGrant.revoked_at.is_(None),
                DelegationGrant.expires_at > now,
                DelegationGrant.used_actions < DelegationGrant.max_actions,
            )
            .order_by(DelegationGrant.expires_at, DelegationGrant.id)
        )
        if consume:
            query = query.with_for_update()
        for grant in (await db.scalars(query)).all():
            if action not in grant.actions or not arguments_match(grant.constraints, arguments):
                continue
            if consume:
                grant.used_actions += 1
                await log_action(
                    db,
                    action="agent.delegation.used",
                    entity_type="delegation",
                    entity_id=grant.id,
                    user_id=owner,
                    details={"tool": action, "used_actions": grant.used_actions},
                )
                await db.commit()
            return grant.id
    return None
