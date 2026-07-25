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


async def hidden_mailbox_names(db: AsyncSession, user: UserInfo | None) -> list[str]:
    """Personal mailbox addresses `user` may NOT read (all but their own)."""
    rows = (
        await db.execute(
            select(MailboxConfig.name, MailboxConfig.owner_sub).where(
                MailboxConfig.mailbox_type == "personal"
            )
        )
    ).all()
    sub = user.sub if user else None
    return [name for name, owner_sub in rows if owner_sub != sub or sub is None]


async def mailbox_filter(
    db: AsyncSession, user: UserInfo | None, *, mailbox_col: ColumnElement
) -> ColumnElement | None:
    """WHERE clause hiding other people's personal mailboxes (None = no filter)."""
    hidden = await hidden_mailbox_names(db, user)
    if not hidden:
        return None
    return mailbox_col.notin_(hidden)


async def may_read_mailbox(db: AsyncSession, user: UserInfo | None, mailbox: str) -> bool:
    """True when `user` may read messages of `mailbox` (by name/address)."""
    return mailbox not in await hidden_mailbox_names(db, user)
