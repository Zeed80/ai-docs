"""Add saved_queries, auto_approval_rules, norm_cards tables.

Revision ID: 2bc89634b9df
Revises: a3b4c5d6e7f8, b4c5d6e7f8a9, c6d7e8f9a0b1, 9c0d1e2f3a4b
Create Date: 2026-05-01

Merge migration — reconciles all branch heads.
Tables saved_queries, auto_approval_rules, norm_cards were created in a prior
session and already exist in the database. This stub records the state.
"""

from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa

revision: str = "2bc89634b9df"
down_revision: Union[tuple, None] = (
    "a3b4c5d6e7f8",
    "b4c5d6e7f8a9",
    "c6d7e8f9a0b1",
    "9c0d1e2f3a4b",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Tables already exist — conditional create for idempotency.
    connection = op.get_bind()

    if not connection.dialect.has_table(connection, "saved_queries"):
        op.create_table(
            "saved_queries",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(100), nullable=False),
            sa.Column("nl_text", sa.Text, nullable=False),
            sa.Column("structured_query", sa.JSON, nullable=True),
            sa.Column("result_count", sa.Integer, nullable=True),
            sa.Column("is_alert", sa.Boolean, default=False),
            sa.Column("alert_cron", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not connection.dialect.has_table(connection, "auto_approval_rules"):
        op.create_table(
            "auto_approval_rules",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("is_active", sa.Boolean, default=True),
            sa.Column("supplier_id", sa.String(36), nullable=True),
            sa.Column("doc_type", sa.String(100), nullable=True),
            sa.Column("max_amount", sa.Float, nullable=True),
            sa.Column("currency", sa.String(10), nullable=True),
            sa.Column("min_trust_score", sa.Float, nullable=True),
            sa.Column("approval_role", sa.String(100), default="auto"),
            sa.Column("created_by", sa.String(100), nullable=True),
            sa.Column("apply_count", sa.Integer, default=0),
            sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not connection.dialect.has_table(connection, "norm_cards"):
        op.create_table(
            "norm_cards",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("entity_type", sa.String(50), nullable=False),
            sa.Column("entity_id", sa.String(36), nullable=False),
            sa.Column("field_name", sa.String(100), nullable=False),
            sa.Column("original_value", sa.Text, nullable=True),
            sa.Column("normalized_value", sa.Text, nullable=True),
            sa.Column("status", sa.String(20), default="pending"),
            sa.Column("reviewed_by", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    pass
