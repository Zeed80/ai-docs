"""Make reminders.entity_type/entity_id nullable (free-standing reminders).

Found via live agent test: a reminder not tied to any invoice/supplier/
document ("напомни мне завтра проверить остатки на складе") had no way to
satisfy the previously-required entity_type/entity_id, forcing the model to
fabricate a placeholder UUID.

Revision ID: 20260819_0008
Revises: 20260819_0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.db.base import GUID

revision = "20260819_0008"
down_revision = "20260819_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("reminders", "entity_type", existing_type=sa.String(length=50), nullable=True)
    op.alter_column("reminders", "entity_id", existing_type=GUID(), nullable=True)


def downgrade() -> None:
    op.alter_column("reminders", "entity_id", existing_type=GUID(), nullable=False)
    op.alter_column("reminders", "entity_type", existing_type=sa.String(length=50), nullable=False)
