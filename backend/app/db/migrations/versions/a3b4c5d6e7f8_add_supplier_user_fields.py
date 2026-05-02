"""add user_notes and user_rating to parties

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-05-02 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("parties", sa.Column("user_notes", sa.Text(), nullable=True))
    op.add_column("parties", sa.Column("user_rating", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("parties", "user_rating")
    op.drop_column("parties", "user_notes")
