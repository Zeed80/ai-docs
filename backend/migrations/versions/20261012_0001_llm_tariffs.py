"""T4: versioned LLM tariffs; a receipt copies the version in force.

Revision ID: 20261012_0001
Revises: 20261010_0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261012_0001"
down_revision = "20261010_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_tariffs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(255), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("input_per_1m", sa.Numeric(20, 8)),
        sa.Column("output_per_1m", sa.Numeric(20, 8)),
        sa.Column("free", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(200), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.String(200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("provider", "model", "effective_from", name="uq_llm_tariff_version"),
    )
    op.create_index("ix_llm_tariffs_lookup", "llm_tariffs", ["provider", "model", "effective_from"])


def downgrade() -> None:
    op.drop_index("ix_llm_tariffs_lookup", table_name="llm_tariffs")
    op.drop_table("llm_tariffs")
