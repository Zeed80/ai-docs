"""Add owned artifacts, delegations and channel identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260910_0001"
down_revision = "20260907_0001"
branch_labels = None
depends_on = None


def _base():
    return [
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("owner_key", sa.String(200), nullable=False),
    ]


def upgrade():
    op.create_table(
        "owned_workspace_blocks",
        *_base(),
        sa.Column("block_key", sa.String(300), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_key", "block_key"),
    )
    op.create_table(
        "agent_delegation_grants",
        *_base(),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("constraints", sa.JSON(), nullable=False),
        sa.Column("max_actions", sa.Integer(), nullable=False),
        sa.Column("used_actions", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "agent_channel_identities",
        *_base(),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.UniqueConstraint("channel", "external_id"),
    )
    op.create_table(
        "agent_script_runs",
        *_base(),
        sa.Column("work_order_id", sa.String(64), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
    )
    for name in (
        "owned_workspace_blocks",
        "agent_delegation_grants",
        "agent_channel_identities",
        "agent_script_runs",
    ):
        op.create_index(f"ix_{name}_owner_key", name, ["owner_key"])
    op.create_index("ix_agent_script_runs_work_order_id", "agent_script_runs", ["work_order_id"])


def downgrade():
    for name in (
        "agent_script_runs",
        "agent_channel_identities",
        "agent_delegation_grants",
        "owned_workspace_blocks",
    ):
        op.drop_table(name)
