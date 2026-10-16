"""Self-hosted mail server retired: drop its connection and provisioning columns.

mail_server_config keeps the company mail domain, the webmail link and the mail
policies (auto-send, attachment limits and retention).

Revision ID: 20261016_0001
Revises: 20261012_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20261016_0001"
down_revision = "20261012_0001"
branch_labels = None
depends_on = None

_DROPPED = (
    "api_url",
    "api_key_encrypted",
    "imap_host",
    "imap_port",
    "smtp_host",
    "smtp_port",
    "default_quota_mb",
)


def upgrade() -> None:
    for column in _DROPPED:
        op.execute(f"ALTER TABLE mail_server_config DROP COLUMN IF EXISTS {column}")


def downgrade() -> None:
    op.add_column("mail_server_config", sa.Column("api_url", sa.String(500)))
    op.add_column("mail_server_config", sa.Column("api_key_encrypted", sa.Text()))
    op.add_column("mail_server_config", sa.Column("imap_host", sa.String(255)))
    op.add_column(
        "mail_server_config",
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
    )
    op.add_column("mail_server_config", sa.Column("smtp_host", sa.String(255)))
    op.add_column(
        "mail_server_config",
        sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="465"),
    )
    op.add_column(
        "mail_server_config",
        sa.Column("default_quota_mb", sa.Integer(), nullable=False, server_default="1024"),
    )
