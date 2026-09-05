"""Ф0.4: configurable outbound attachment size cap.

Revision ID: 20260828_0005
Revises: 20260828_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0005"
down_revision: str | None = "20260828_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mail_server_config")}
    if "max_attachment_mb" not in cols:
        op.add_column(
            "mail_server_config",
            sa.Column("max_attachment_mb", sa.Integer(), nullable=False, server_default="25"),
        )


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("mail_server_config")}
    if "max_attachment_mb" in cols:
        op.drop_column("mail_server_config", "max_attachment_mb")
