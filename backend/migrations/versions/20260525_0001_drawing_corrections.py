"""Drawing feature corrections table for few-shot learning loop."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260525_0001"
down_revision = "20260524_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa_inspect(op.get_bind()).has_table("drawing_feature_corrections"):
        return

    op.create_table(
        "drawing_feature_corrections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("drawing_id", sa.UUID(), nullable=False),
        sa.Column("feature_id", sa.UUID(), nullable=True),
        sa.Column("original_type", sa.String(100), nullable=False),
        sa.Column("corrected_type", sa.String(100), nullable=False),
        sa.Column("original_name", sa.String(300), nullable=False),
        sa.Column("corrected_name", sa.String(300), nullable=True),
        sa.Column("confidence_at_correction", sa.Float(), nullable=False),
        sa.Column("drawing_type", sa.String(50), nullable=False),
        sa.Column("source_view", sa.String(50), nullable=True),
        sa.Column("context_json", sa.JSON(), nullable=True),
        sa.Column("corrected_by", sa.String(100), nullable=False),
        sa.Column("used_as_few_shot", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["drawing_id"], ["drawings.id"]),
        sa.ForeignKeyConstraint(["feature_id"], ["drawing_features.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_drawing_feature_corrections_drawing_id", "drawing_feature_corrections", ["drawing_id"])
    op.create_index("ix_dfc_drawing_type_corrected", "drawing_feature_corrections", ["drawing_type", "corrected_type"])


def downgrade() -> None:
    op.drop_index("ix_dfc_drawing_type_corrected", table_name="drawing_feature_corrections")
    op.drop_index("ix_drawing_feature_corrections_drawing_id", table_name="drawing_feature_corrections")
    op.drop_table("drawing_feature_corrections")
