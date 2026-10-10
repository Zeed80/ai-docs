"""E34: browser secrets and the grant's secret ids.

Revision ID: 20261010_0005
Revises: 20261010_0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261010_0005"
down_revision = "20261010_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_secrets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_sub", sa.String(200), nullable=False, index=True),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("origin", sa.String(500), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.add_column(
        "computer_use_grants",
        sa.Column("secret_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("computer_use_grants", "secret_ids")
    op.drop_table("browser_secrets")
