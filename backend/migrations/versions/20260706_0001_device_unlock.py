"""Device unlock credentials for biometric/PIN quick-login.

Revision ID: 20260706_0001
Revises: 20260705_0001
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260706_0001"
down_revision = "20260705_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())

    if not insp.has_table("device_unlock_credentials"):
        op.create_table(
            "device_unlock_credentials",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("handle", sa.String(64), nullable=False),
            sa.Column("user_sub", sa.String(255), nullable=False),
            sa.Column("secret_hash", sa.String(128), nullable=False),
            sa.Column("method", sa.String(20), nullable=False, server_default="biometric"),
            sa.Column("label", sa.String(120), nullable=True),
            sa.Column("platform", sa.String(20), nullable=False, server_default="android"),
            sa.Column("app_version", sa.String(50), nullable=True),
            sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("handle", name="uq_device_unlock_handle"),
        )
        op.create_index(
            "ix_device_unlock_credentials_handle", "device_unlock_credentials", ["handle"]
        )
        op.create_index(
            "ix_device_unlock_credentials_user_sub", "device_unlock_credentials", ["user_sub"]
        )


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if insp.has_table("device_unlock_credentials"):
        op.drop_table("device_unlock_credentials")
