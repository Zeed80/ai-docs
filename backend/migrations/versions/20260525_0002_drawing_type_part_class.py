"""Add drawing_type and part_class columns to drawings table for two-stage VLM pipeline."""

import sqlalchemy as sa
from alembic import op

revision = "20260525_0002"
down_revision = "20260525_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("drawings")}
    if "drawing_type" not in cols:
        op.add_column("drawings", sa.Column("drawing_type", sa.String(30), nullable=True))
        op.create_index("ix_drawings_drawing_type", "drawings", ["drawing_type"])
    if "part_class" not in cols:
        op.add_column("drawings", sa.Column("part_class", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_index("ix_drawings_drawing_type", table_name="drawings")
    op.drop_column("drawings", "drawing_type")
    op.drop_column("drawings", "part_class")
