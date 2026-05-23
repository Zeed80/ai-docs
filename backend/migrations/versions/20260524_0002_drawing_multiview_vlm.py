"""Drawing multiview VLM: is_confidential, new feature types, view sections, assembly BOM."""

import sqlalchemy as sa
from alembic import op

revision = "20260524_0002"
down_revision = "20260524_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Drawing.is_confidential ───────────────────────────────────────────────
    op.add_column(
        "drawings",
        sa.Column("is_confidential", sa.Boolean(), nullable=False, server_default="true"),
    )

    # ── DrawingFeature multi-view provenance fields ───────────────────────────
    op.add_column("drawing_features", sa.Column("source_view", sa.String(50), nullable=True))
    op.add_column("drawing_features", sa.Column("confirmed_by_views", sa.JSON(), nullable=True))
    op.add_column(
        "drawing_features",
        sa.Column("confidence_votes", sa.Integer(), nullable=False, server_default="1"),
    )

    # ── New DrawingFeatureType enum values ────────────────────────────────────
    op.execute("ALTER TYPE drawingfeaturetype ADD VALUE IF NOT EXISTS 'weld'")
    op.execute("ALTER TYPE drawingfeaturetype ADD VALUE IF NOT EXISTS 'knurl'")
    op.execute("ALTER TYPE drawingfeaturetype ADD VALUE IF NOT EXISTS 'key_slot'")
    op.execute("ALTER TYPE drawingfeaturetype ADD VALUE IF NOT EXISTS 'spline'")
    op.execute("ALTER TYPE drawingfeaturetype ADD VALUE IF NOT EXISTS 'center_bore'")

    # ── DrawingViewSection table ──────────────────────────────────────────────
    op.create_table(
        "drawing_view_sections",
        sa.Column("id", sa.UUID(), nullable=False, primary_key=True),
        sa.Column("drawing_id", sa.UUID(), sa.ForeignKey("drawings.id"), nullable=False),
        sa.Column("section_label", sa.String(20), nullable=True),
        sa.Column("section_type", sa.String(30), nullable=False),
        sa.Column("bbox_on_sheet", sa.JSON(), nullable=True),
        sa.Column("image_path", sa.String(1000), nullable=True),
        sa.Column("cutting_plane_label", sa.String(20), nullable=True),
        sa.Column("cutting_plane_coords", sa.JSON(), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_drawing_view_sections_drawing_id", "drawing_view_sections", ["drawing_id"])
    op.create_index("ix_drawing_view_sections_section_type", "drawing_view_sections", ["section_type"])

    # ── DrawingAssemblyBOM table ──────────────────────────────────────────────
    op.create_table(
        "drawing_assembly_boms",
        sa.Column("id", sa.UUID(), nullable=False, primary_key=True),
        sa.Column("drawing_id", sa.UUID(), sa.ForeignKey("drawings.id"), nullable=False),
        sa.Column("item_no", sa.Integer(), nullable=False),
        sa.Column("designation", sa.String(500), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(20), nullable=True),
        sa.Column("material", sa.String(300), nullable=True),
        sa.Column("drawing_number", sa.String(200), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("balloon_coords", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_drawing_assembly_boms_drawing_id", "drawing_assembly_boms", ["drawing_id"])
    op.create_index(
        "ix_drawing_assembly_boms_drawing_item",
        "drawing_assembly_boms",
        ["drawing_id", "item_no"],
    )


def downgrade() -> None:
    op.drop_table("drawing_assembly_boms")
    op.drop_table("drawing_view_sections")
    op.drop_column("drawing_features", "confidence_votes")
    op.drop_column("drawing_features", "confirmed_by_views")
    op.drop_column("drawing_features", "source_view")
    op.drop_column("drawings", "is_confidential")
    # PostgreSQL does not support removing enum values; new feature types remain.
