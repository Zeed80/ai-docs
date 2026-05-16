"""add users and api_keys tables

Revision ID: b5c6d7e8f9a0
Revises: b4c5d6e7f8a9, 20260508_0003
Create Date: 2026-05-14 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "b5c6d7e8f9a0"
down_revision: Union[tuple, None] = ("b4c5d6e7f8a9", "20260508_0003")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("sub", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("preferred_username", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("role", sa.String(length=50), nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("preferences", sa.JSON(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sub", name="uq_users_sub"),
    )
    op.create_index(op.f("ix_users_sub"), "users", ["sub"], unique=True)
    op.create_index(op.f("ix_users_email"), "users", ["email"])
    op.create_index(op.f("ix_users_role"), "users", ["role"])

    op.create_table(
        "api_keys",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("user_sub", sa.String(length=255), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index(op.f("ix_api_keys_user_sub"), "api_keys", ["user_sub"])


def downgrade() -> None:
    op.drop_index(op.f("ix_api_keys_user_sub"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index(op.f("ix_users_role"), table_name="users")
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_index(op.f("ix_users_sub"), table_name="users")
    op.drop_table("users")
