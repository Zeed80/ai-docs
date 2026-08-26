"""mailbox OAuth2 authentication (Gmail / Microsoft 365)

Gmail and, for most Microsoft 365 tenants, Outlook no longer accept the plain
account password for IMAP/SMTP. This adds an OAuth2 path alongside the
existing password/app-password one: each mailbox can now carry its own
refresh/access token, and admins register one Client ID/Secret per provider
(google, microsoft) that every mailbox's consent flow runs against.

Revision ID: 20260826_0001
Revises: 20260823_0001
Create Date: 2026-08-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0001"
down_revision: Union[str, None] = "20260823_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "mailbox_configs",
        sa.Column("auth_method", sa.String(20), nullable=False, server_default="password"),
    )
    op.add_column("mailbox_configs", sa.Column("oauth_provider", sa.String(20), nullable=True))
    op.add_column("mailbox_configs", sa.Column("oauth_refresh_token_encrypted", sa.Text(), nullable=True))
    op.add_column("mailbox_configs", sa.Column("oauth_access_token_encrypted", sa.Text(), nullable=True))
    op.add_column("mailbox_configs", sa.Column("oauth_token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("mailbox_configs", sa.Column("oauth_scope", sa.String(500), nullable=True))
    op.add_column("mailbox_configs", sa.Column("oauth_email", sa.String(500), nullable=True))

    op.create_table(
        "oauth_app_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("client_id", sa.String(500), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("redirect_uri", sa.String(500), nullable=True),
        sa.Column("updated_by", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("oauth_app_configs")
    op.drop_column("mailbox_configs", "oauth_email")
    op.drop_column("mailbox_configs", "oauth_scope")
    op.drop_column("mailbox_configs", "oauth_token_expires_at")
    op.drop_column("mailbox_configs", "oauth_access_token_encrypted")
    op.drop_column("mailbox_configs", "oauth_refresh_token_encrypted")
    op.drop_column("mailbox_configs", "oauth_provider")
    op.drop_column("mailbox_configs", "auth_method")
