"""E42: immutable workspace block revisions.

Revision ID: 20261010_0004
Revises: 20261010_0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261010_0004"
down_revision = "20261010_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "owned_workspace_blocks",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "owned_workspace_block_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_key", sa.String(200), nullable=False, index=True),
        sa.Column("block_key", sa.String(300), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_key", "block_key", "revision"),
    )


def downgrade() -> None:
    op.drop_table("owned_workspace_block_versions")
    op.drop_column("owned_workspace_blocks", "revision")
