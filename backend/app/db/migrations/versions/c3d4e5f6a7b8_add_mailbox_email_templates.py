"""add mailbox_configs and email_templates tables

Revision ID: c3d4e5f6a7b8
Revises: a1b2c3d4e5f6, a3b4c5d6e7f8
Create Date: 2026-05-03 00:00:00.000000
"""

from typing import Sequence, Tuple, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[Tuple[str, ...], str, None] = ("a1b2c3d4e5f6", "a3b4c5d6e7f8")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mailbox_configs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), onupdate=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("imap_host", sa.String(500), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("imap_user", sa.String(500), nullable=False),
        sa.Column("imap_password_encrypted", sa.Text(), nullable=False),
        sa.Column("imap_ssl", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("imap_folder", sa.String(100), nullable=False, server_default="INBOX"),
        sa.Column("smtp_host", sa.String(500), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_user", sa.String(500), nullable=True),
        sa.Column("smtp_password_encrypted", sa.Text(), nullable=True),
        sa.Column("smtp_use_tls", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("smtp_from_address", sa.String(500), nullable=True),
        sa.Column("smtp_from_name", sa.String(200), nullable=True),
        sa.Column("default_doc_type", sa.String(50), nullable=True),
        sa.Column("assigned_role", sa.String(50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_mailbox_configs_name", "mailbox_configs", ["name"], unique=True)
    op.create_index("ix_mailbox_configs_is_active", "mailbox_configs", ["is_active"])

    op.create_table(
        "email_templates",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), onupdate=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("category", sa.Enum(
            "payment", "inquiry", "confirmation", "reminder", "request", "custom",
            name="emailtemplatecategory",
        ), nullable=False, server_default="custom"),
        sa.Column("language", sa.String(10), nullable=False, server_default="ru"),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("variables", sa.JSON(), nullable=True),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("source_email_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        sa.ForeignKeyConstraint(["source_email_id"], ["email_messages.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_email_templates_slug", "email_templates", ["slug"], unique=True)
    op.create_index("ix_email_templates_is_builtin", "email_templates", ["is_builtin"])


def downgrade() -> None:
    op.drop_table("email_templates")
    op.drop_index("ix_mailbox_configs_is_active", table_name="mailbox_configs")
    op.drop_index("ix_mailbox_configs_name", table_name="mailbox_configs")
    op.drop_table("mailbox_configs")
    op.execute("DROP TYPE IF EXISTS emailtemplatecategory")
