"""Keep channel identity history while allowing safe revoke and rebind."""

import sqlalchemy as sa
from alembic import op

revision = "20260929_0003"
down_revision = "20260929_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_channel_identities",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.drop_constraint(
        "agent_channel_identities_channel_external_id_key",
        "agent_channel_identities",
        type_="unique",
    )
    op.create_index(
        "uq_active_agent_channel_identity",
        "agent_channel_identities",
        ["channel", "external_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.add_column(
        "durable_chat_runs",
        sa.Column(
            "source_binding_id",
            sa.UUID(),
            sa.ForeignKey("agent_channel_identities.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )


def downgrade():
    op.drop_column("durable_chat_runs", "source_binding_id")
    op.drop_index("uq_active_agent_channel_identity", table_name="agent_channel_identities")
    op.create_unique_constraint(
        "agent_channel_identities_channel_external_id_key",
        "agent_channel_identities",
        ["channel", "external_id"],
    )
    op.drop_column("agent_channel_identities", "is_active")
