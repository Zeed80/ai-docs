"""Bind agent cron schedules to their human owner."""

import sqlalchemy as sa
from alembic import op

revision = "20260929_0005"
down_revision = "20260929_0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_crons", sa.Column("owner_key", sa.String(200), nullable=True))
    op.create_index("ix_agent_crons_owner_key", "agent_crons", ["owner_key"])
    op.add_column(
        "agent_crons",
        sa.Column(
            "delegation_grant_id",
            sa.Uuid(),
            sa.ForeignKey("agent_delegation_grants.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index("ix_agent_crons_delegation_grant_id", "agent_crons", ["delegation_grant_id"])


def downgrade():
    op.drop_index("ix_agent_crons_delegation_grant_id", table_name="agent_crons")
    op.drop_column("agent_crons", "delegation_grant_id")
    op.drop_index("ix_agent_crons_owner_key", table_name="agent_crons")
    op.drop_column("agent_crons", "owner_key")
