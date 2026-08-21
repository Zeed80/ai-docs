"""supplier catalog documents: doc type + source document link

Revision ID: 20260821_0001
Revises: 20260820_0002
Create Date: 2026-08-21
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260821_0001"
down_revision: Union[str, None] = "20260820_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
    # PostgreSQL; alembic runs in one, so use autocommit for this statement.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE documenttype ADD VALUE IF NOT EXISTS 'supplier_catalog'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum; the value simply stops
    # being used. Safe to leave in place.
    pass
