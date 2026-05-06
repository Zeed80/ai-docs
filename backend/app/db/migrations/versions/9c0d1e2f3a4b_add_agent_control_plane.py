"""add agent control plane

Revision ID: 9c0d1e2f3a4b
Revises: 8b9c0d1e2f3a
Create Date: 2026-05-06 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "9c0d1e2f3a4b"
down_revision: Union[str, None] = "8b9c0d1e2f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid() -> sa.types.TypeEngine:
    return PG_UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "agent_config_proposals",
        sa.Column("setting_path", sa.String(length=200), nullable=False),
        sa.Column("proposed_value", sa.JSON(), nullable=True),
        sa.Column("current_value", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(length=30), nullable=False),
        sa.Column("protected", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_config_proposals_setting_path", "agent_config_proposals", ["setting_path"])
    op.create_index("ix_agent_config_proposals_status", "agent_config_proposals", ["status"])

    op.create_table(
        "agent_teams",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_teams_status", "agent_teams", ["status"])

    op.create_table(
        "agent_tasks",
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("team_id", _uuid(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["agent_teams.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_tasks_role", "agent_tasks", ["role"])
    op.create_index("ix_agent_tasks_status", "agent_tasks", ["status"])

    op.create_table(
        "agent_crons",
        sa.Column("schedule", sa.String(length=120), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_crons_enabled", "agent_crons", ["enabled"])

    op.create_table(
        "agent_plugins",
        sa.Column("plugin_key", sa.String(length=200), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("risk_level", sa.String(length=30), nullable=False),
        sa.Column("installed_by", sa.String(length=100), nullable=False),
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_key"),
    )
    op.create_index("ix_agent_plugins_plugin_key", "agent_plugins", ["plugin_key"])
    op.create_index("ix_agent_plugins_enabled", "agent_plugins", ["enabled"])

    op.create_table(
        "capability_proposals",
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("missing_capability", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("suggested_artifact", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("risk_level", sa.String(length=30), nullable=False),
        sa.Column("sandbox_status", sa.String(length=30), nullable=False),
        sa.Column("test_status", sa.String(length=30), nullable=False),
        sa.Column("audit_status", sa.String(length=30), nullable=False),
        sa.Column("draft", sa.JSON(), nullable=False),
        sa.Column("rollback_plan", sa.JSON(), nullable=True),
        sa.Column("requested_by", sa.String(length=100), nullable=False),
        sa.Column("decided_by", sa.String(length=100), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_capability_proposals_status", "capability_proposals", ["status"])

    op.create_table(
        "memory_facts",
        sa.Column("scope", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", _uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_facts_scope", "memory_facts", ["scope"])
    op.create_index("ix_memory_facts_kind", "memory_facts", ["kind"])
    op.create_index("ix_memory_facts_title", "memory_facts", ["title"])
    op.create_index("ix_memory_facts_pinned", "memory_facts", ["pinned"])


def downgrade() -> None:
    op.drop_index("ix_memory_facts_pinned", table_name="memory_facts")
    op.drop_index("ix_memory_facts_title", table_name="memory_facts")
    op.drop_index("ix_memory_facts_kind", table_name="memory_facts")
    op.drop_index("ix_memory_facts_scope", table_name="memory_facts")
    op.drop_table("memory_facts")
    op.drop_index("ix_agent_plugins_enabled", table_name="agent_plugins")
    op.drop_index("ix_agent_plugins_plugin_key", table_name="agent_plugins")
    op.drop_table("agent_plugins")
    op.drop_index("ix_capability_proposals_status", table_name="capability_proposals")
    op.drop_table("capability_proposals")
    op.drop_index("ix_agent_crons_enabled", table_name="agent_crons")
    op.drop_table("agent_crons")
    op.drop_index("ix_agent_tasks_status", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_role", table_name="agent_tasks")
    op.drop_table("agent_tasks")
    op.drop_index("ix_agent_teams_status", table_name="agent_teams")
    op.drop_table("agent_teams")
    op.drop_index("ix_agent_config_proposals_status", table_name="agent_config_proposals")
    op.drop_index("ix_agent_config_proposals_setting_path", table_name="agent_config_proposals")
    op.drop_table("agent_config_proposals")
