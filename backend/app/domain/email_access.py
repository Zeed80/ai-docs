"""Row-level visibility for e-mail: personal mailboxes are private.

Shared/integration mailboxes (procurement, accounting, general — anything with
``mailbox_type != "personal"``) keep their previous behaviour: every
authenticated user of the workspace may read them, because that is the point of
a company inbox.

Personal mailboxes (``mailbox_configs.mailbox_type == "personal"``, provisioned
per user by an admin — see backend/app/api/admin.py) are different: their
contents belong to one employee. They are readable **only by their owner**, and
deliberately *not* by admins/managers — provisioning a mailbox is an admin
action, reading someone's private correspondence is not. Widening that rule is
a product decision, not a bug fix; it belongs in the mailbox settings UI, not
in a role check here.

The agent reaches these endpoints through the ``agent-service`` account with an
``X-Acting-User`` header (app.auth.acting.get_effective_user). A headless turn
without an acting user therefore resolves to the service account, which owns no
personal mailbox and consequently sees none — fail-closed.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.auth.models import UserInfo
from app.db.models import MailboxConfig


async def hidden_mailbox_names(
    db: AsyncSession, user: UserInfo | None, *, for_agent: bool = False
) -> list[str]:
    """Personal mailbox addresses `user` may NOT read (all but their own).

    ``for_agent=True`` additionally hides the user's *own* personal mailbox when
    they have not switched on ``sweep_enabled`` — the consent gate for AI
    reading a private inbox is independent of the human's own access to it.
    """
    rows = (
        await db.execute(
            select(
                MailboxConfig.name, MailboxConfig.owner_sub, MailboxConfig.sweep_enabled
            ).where(MailboxConfig.mailbox_type == "personal")
        )
    ).all()
    sub = user.sub if user else None
    hidden: list[str] = []
    for name, owner_sub, sweep_enabled in rows:
        if owner_sub != sub or sub is None:
            hidden.append(name)
        elif for_agent and not sweep_enabled:
            hidden.append(name)
    return hidden


async def mailbox_filter(
    db: AsyncSession, user: UserInfo | None, *, mailbox_col: ColumnElement, for_agent: bool = False
) -> ColumnElement | None:
    """WHERE clause hiding other people's personal mailboxes (None = no filter)."""
    hidden = await hidden_mailbox_names(db, user, for_agent=for_agent)
    if not hidden:
        return None
    return mailbox_col.notin_(hidden)


async def may_read_mailbox(
    db: AsyncSession, user: UserInfo | None, mailbox: str, *, for_agent: bool = False
) -> bool:
    """True when `user` may read messages of `mailbox` (by name/address)."""
    return mailbox not in await hidden_mailbox_names(db, user, for_agent=for_agent)


async def may_write_mailbox(db: AsyncSession, user: UserInfo | None, mailbox: str) -> bool:
    """True when `user` may SEND from `mailbox`.

    Shared mailboxes: any authenticated user (same rule as reading). Personal
    mailboxes: only the owner. The agent-service account (no acting user) owns
    no personal mailbox and may only send from shared ones.
    """
    row = (
        await db.execute(
            select(MailboxConfig.mailbox_type, MailboxConfig.owner_sub).where(
                MailboxConfig.name == mailbox
            )
        )
    ).first()
    if row is None:
        return False
    mailbox_type, owner_sub = row
    if mailbox_type != "personal":
        return True
    return bool(user) and owner_sub == user.sub
