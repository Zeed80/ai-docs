"""Allow standalone EMG roots and link CadIR to its source graph revision.

Revision ID: 20260809_0002
Revises: 20260809_0001
Create Date: 2026-08-09
"""

import sqlalchemy as sa
from alembic import op

from app.db.base import GUID

revision = "20260809_0002"
down_revision = "20260809_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("engineering_graph_revisions", "engineering_project_id", nullable=True)
    op.add_column(
        "cad_ir_revisions",
        sa.Column(
            "engineering_graph_revision_id",
            GUID(),
            sa.ForeignKey("engineering_graph_revisions.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_cad_ir_revisions_engineering_graph_revision_id",
        "cad_ir_revisions",
        ["engineering_graph_revision_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cad_ir_revisions_engineering_graph_revision_id",
        table_name="cad_ir_revisions",
    )
    op.drop_column("cad_ir_revisions", "engineering_graph_revision_id")
    op.alter_column("engineering_graph_revisions", "engineering_project_id", nullable=False)
