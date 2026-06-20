"""Durable assignments: task_routing overrides + agent_config mirror.

Redis-only task_routing/agent_config reverted to YAML defaults on a Redis flush.
These tables make assignments durable (hydrated into Redis on startup).

Revision ID: 20260619_0004
Revises: 20260619_0003
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260619_0004"
down_revision = "20260619_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())

    if not insp.has_table("task_routing_overrides"):
        op.create_table(
            "task_routing_overrides",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("task", sa.String(80), nullable=False),
            sa.Column("routing", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task", name="uq_task_routing_overrides_task"),
        )

    if not insp.has_table("agent_config_store"):
        op.create_table(
            "agent_config_store",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("singleton_key", sa.String(50), nullable=False, server_default="default"),
            sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("singleton_key", name="uq_agent_config_store_singleton"),
        )


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if insp.has_table("agent_config_store"):
        op.drop_table("agent_config_store")
    if insp.has_table("task_routing_overrides"):
        op.drop_table("task_routing_overrides")
