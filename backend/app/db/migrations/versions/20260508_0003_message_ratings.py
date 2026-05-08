"""Add message_ratings table for user thumbs up/down feedback on agent responses.

Revision ID: 20260508_0003
Revises: 9c0d1e2f3a4b
Create Date: 2026-05-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260508_0003"
down_revision: Union[str, None] = "9c0d1e2f3a4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "message_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(120), nullable=False),
        sa.Column("message_id", sa.String(120), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("tools_used", sa.JSON(), nullable=True),
        sa.Column("comment", sa.String(500), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_message_ratings_session_id", "message_ratings", ["session_id"])
    op.create_index("ix_message_ratings_message_id", "message_ratings", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_message_ratings_message_id", table_name="message_ratings")
    op.drop_index("ix_message_ratings_session_id", table_name="message_ratings")
    op.drop_table("message_ratings")
