"""add agent tool call approval type

Revision ID: 2c3d4e5f6071
Revises: 1b2c3d4e5f60
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "2c3d4e5f6071"
down_revision: Union[str, None] = "1b2c3d4e5f60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS 'agent_tool_call'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely without recreating the type.
    pass
