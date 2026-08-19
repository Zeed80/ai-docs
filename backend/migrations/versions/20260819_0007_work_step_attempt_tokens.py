"""Add tokens_used/cost_usd to work_step_attempts for budget enforcement (Б15).

Revision ID: 20260819_0007
Revises: 20260817_0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260819_0007"
down_revision = "20260817_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_step_attempts", sa.Column("tokens_used", sa.Integer()))
    op.add_column("work_step_attempts", sa.Column("cost_usd", sa.Float()))


def downgrade() -> None:
    op.drop_column("work_step_attempts", "cost_usd")
    op.drop_column("work_step_attempts", "tokens_used")
