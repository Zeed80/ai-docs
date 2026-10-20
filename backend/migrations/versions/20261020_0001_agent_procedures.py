"""Procedures the agent proposes and the owner approves (hermes-agent's skills).

Revision ID: 20261020_0001
Revises: 20261016_0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261020_0001"
down_revision = "20261016_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_procedures",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("source_work_order_id", UUID(as_uuid=True)),
        sa.Column("proposed_by", sa.String(255), nullable=False),
        sa.Column("decided_by", sa.String(255)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        if_not_exists=True,
    )
    op.create_index("ix_agent_procedures_name", "agent_procedures", ["name"], if_not_exists=True)
    op.create_index(
        "ix_agent_procedures_status", "agent_procedures", ["status"], if_not_exists=True
    )
    op.create_index(
        "uq_agent_procedures_live_name",
        "agent_procedures",
        ["name"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'stale')"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("agent_procedures")
