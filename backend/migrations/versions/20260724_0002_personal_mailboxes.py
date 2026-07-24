"""Personal (per-user) mailboxes.

Adds ``mailbox_configs.owner_sub`` (nullable, indexed) and
``mailbox_configs.mailbox_type`` ("shared" | "personal", default "shared") so
a MailboxConfig row can represent either a company-wide integration mailbox
(procurement/accounting/general — unchanged) or a personal @<domain> address
provisioned by an admin for a specific user.

Revision ID: 20260724_0002
Revises: 20260724_0001
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260724_0002"
down_revision = "20260724_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "mailbox_configs" in tables:
        columns = {c["name"] for c in insp.get_columns("mailbox_configs")}
        if "owner_sub" not in columns:
            op.add_column("mailbox_configs", sa.Column("owner_sub", sa.String(length=255), nullable=True))
            op.create_index("ix_mailbox_configs_owner_sub", "mailbox_configs", ["owner_sub"])
        if "mailbox_type" not in columns:
            op.add_column(
                "mailbox_configs",
                sa.Column("mailbox_type", sa.String(length=20), nullable=False, server_default="shared"),
            )

    if "mail_server_config" not in tables:
        op.create_table(
            "mail_server_config",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("singleton_key", sa.String(length=50), nullable=False, server_default="default"),
            sa.Column("api_url", sa.String(length=500)),
            sa.Column("api_key_encrypted", sa.Text()),
            sa.Column("mail_domain", sa.String(length=255)),
            sa.Column("webmail_url", sa.String(length=500)),
            sa.Column("imap_host", sa.String(length=255)),
            sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
            sa.Column("smtp_host", sa.String(length=255)),
            sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="465"),
            sa.Column("updated_by", sa.String(length=100)),
        )
        op.create_unique_constraint("uq_mail_server_config_singleton", "mail_server_config", ["singleton_key"])


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "mail_server_config" in tables:
        op.drop_table("mail_server_config")

    if "mailbox_configs" in tables:
        columns = {c["name"] for c in insp.get_columns("mailbox_configs")}
        if "mailbox_type" in columns:
            op.drop_column("mailbox_configs", "mailbox_type")
        if "owner_sub" in columns:
            op.drop_index("ix_mailbox_configs_owner_sub", table_name="mailbox_configs")
            op.drop_column("mailbox_configs", "owner_sub")
