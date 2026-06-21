"""Device registrations for mobile push (self-hosted ntfy).

Revision ID: 20260620_0001
Revises: 20260619_0004
Create Date: 2026-06-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260620_0001"
down_revision = "20260619_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())

    if not insp.has_table("device_registrations"):
        op.create_table(
            "device_registrations",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_sub", sa.String(255), nullable=False),
            sa.Column("ntfy_topic", sa.String(255), nullable=False),
            sa.Column("ntfy_endpoint", sa.String(1000), nullable=True),
            sa.Column("platform", sa.String(20), nullable=False, server_default="android"),
            sa.Column("app_version", sa.String(50), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ntfy_topic", name="uq_device_registrations_topic"),
        )
        op.create_index("ix_device_registrations_user_sub", "device_registrations", ["user_sub"])
        op.create_index("ix_device_registrations_ntfy_topic", "device_registrations", ["ntfy_topic"])


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if insp.has_table("device_registrations"):
        op.drop_table("device_registrations")
