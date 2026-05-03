"""add drawings and tool catalog

Revision ID: 7a8b9c0d1e2f
Revises: c3d4e5f6a7b8
Create Date: 2026-05-03 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "7a8b9c0d1e2f"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enums
    drawing_status = sa.Enum(
        "uploaded", "analyzing", "analyzed", "needs_review", "approved", "failed",
        name="drawingstatus",
    )
    drawing_feature_type = sa.Enum(
        "hole", "pocket", "surface", "boss", "groove", "thread",
        "chamfer", "radius", "slot", "contour", "other",
        name="drawingfeaturetype",
    )
    feature_primitive_type = sa.Enum(
        "circle", "arc", "rectangle", "polyline", "line", "spline", "ellipse",
        name="featureprimitivetype",
    )
    feature_dim_type = sa.Enum(
        "linear", "angular", "diameter", "radius", "depth", "arc_length",
        name="featuredimtype",
    )
    roughness_type = sa.Enum(
        "Ra", "Rz", "Rmax", "Rq",
        name="roughnesstype",
    )
    tool_type_enum = sa.Enum(
        "drill", "endmill", "insert", "holder", "tap", "reamer", "boring_bar",
        "thread_mill", "grinder", "turning_tool", "milling_cutter",
        "countersink", "counterbore", "other",
        name="tooltypeenum",
    )
    tool_source_enum = sa.Enum(
        "warehouse", "catalog", "manual",
        name="toolsourceenum",
    )

    # ── tool_suppliers ────────────────────────────────────────────────────────
    op.create_table(
        "tool_suppliers",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("website", sa.String(length=500), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("contact_info", sa.JSON(), nullable=True),
        sa.Column("catalog_format", sa.String(length=50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_suppliers_name", "tool_suppliers", ["name"])
    op.create_index("ix_tool_suppliers_is_active", "tool_suppliers", ["is_active"])

    # ── tool_catalog_entries ──────────────────────────────────────────────────
    op.create_table(
        "tool_catalog_entries",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("supplier_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("part_number", sa.String(length=200), nullable=True),
        sa.Column("tool_type", tool_type_enum, nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("diameter_mm", sa.Float(), nullable=True),
        sa.Column("length_mm", sa.Float(), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=True),
        sa.Column("material", sa.String(length=200), nullable=True),
        sa.Column("coating", sa.String(length=200), nullable=True),
        sa.Column("price_currency", sa.String(length=3), nullable=False, server_default="RUB"),
        sa.Column("price_value", sa.Float(), nullable=True),
        sa.Column("catalog_page", sa.Integer(), nullable=True),
        sa.Column("source_document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("embedding_id", sa.String(length=200), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["supplier_id"], ["tool_suppliers.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_catalog_entries_supplier_id", "tool_catalog_entries", ["supplier_id"])
    op.create_index("ix_tool_catalog_entries_part_number", "tool_catalog_entries", ["part_number"])
    op.create_index("ix_tool_catalog_entries_tool_type", "tool_catalog_entries", ["tool_type"])
    op.create_index("ix_tool_catalog_entries_name", "tool_catalog_entries", ["name"])
    op.create_index("ix_tool_catalog_entries_diameter_mm", "tool_catalog_entries", ["diameter_mm"])
    op.create_index("ix_tool_catalog_entries_is_active", "tool_catalog_entries", ["is_active"])
    op.create_index("ix_tool_catalog_entries_embedding_id", "tool_catalog_entries", ["embedding_id"])

    # ── drawings ──────────────────────────────────────────────────────────────
    op.create_table(
        "drawings",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("drawing_number", sa.String(length=200), nullable=True),
        sa.Column("revision", sa.String(length=50), nullable=True),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("format", sa.String(length=20), nullable=False),
        sa.Column("svg_path", sa.String(length=1000), nullable=True),
        sa.Column("thumbnail_path", sa.String(length=1000), nullable=True),
        sa.Column("title_block", sa.JSON(), nullable=True),
        sa.Column("bounding_box", sa.JSON(), nullable=True),
        sa.Column("status", drawing_status, nullable=False, server_default="uploaded"),
        sa.Column("analysis_error", sa.Text(), nullable=True),
        sa.Column("celery_task_id", sa.String(length=200), nullable=True),
        sa.Column("embedding_id", sa.String(length=200), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_drawings_document_id", "drawings", ["document_id"])
    op.create_index("ix_drawings_drawing_number", "drawings", ["drawing_number"])
    op.create_index("ix_drawings_status", "drawings", ["status"])
    op.create_index("ix_drawings_embedding_id", "drawings", ["embedding_id"])

    # ── drawing_features ──────────────────────────────────────────────────────
    op.create_table(
        "drawing_features",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("drawing_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("feature_type", drawing_feature_type, nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("ai_raw", sa.JSON(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("embedding_id", sa.String(length=200), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["drawing_id"], ["drawings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_drawing_features_drawing_id", "drawing_features", ["drawing_id"])
    op.create_index("ix_drawing_features_feature_type", "drawing_features", ["feature_type"])
    op.create_index("ix_drawing_features_embedding_id", "drawing_features", ["embedding_id"])

    # ── feature_contours ──────────────────────────────────────────────────────
    op.create_table(
        "feature_contours",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("feature_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("primitive_type", feature_primitive_type, nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("layer", sa.String(length=100), nullable=True),
        sa.Column("line_type", sa.String(length=30), nullable=False, server_default="solid"),
        sa.Column("color", sa.String(length=30), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_user_edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["feature_id"], ["drawing_features.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feature_contours_feature_id", "feature_contours", ["feature_id"])

    # ── feature_dimensions ────────────────────────────────────────────────────
    op.create_table(
        "feature_dimensions",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("feature_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("dim_type", feature_dim_type, nullable=False),
        sa.Column("nominal", sa.Float(), nullable=False),
        sa.Column("upper_tol", sa.Float(), nullable=True),
        sa.Column("lower_tol", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=20), nullable=False, server_default="mm"),
        sa.Column("fit_system", sa.String(length=20), nullable=True),
        sa.Column("label", sa.String(length=200), nullable=True),
        sa.Column("annotation_position", sa.JSON(), nullable=True),
        sa.Column("is_reference", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["feature_id"], ["drawing_features.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feature_dimensions_feature_id", "feature_dimensions", ["feature_id"])

    # ── feature_surfaces ──────────────────────────────────────────────────────
    op.create_table(
        "feature_surfaces",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("feature_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("roughness_type", roughness_type, nullable=False, server_default="Ra"),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("direction", sa.String(length=50), nullable=True),
        sa.Column("lay_symbol", sa.String(length=10), nullable=True),
        sa.Column("machining_required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("annotation_position", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["feature_id"], ["drawing_features.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feature_surfaces_feature_id", "feature_surfaces", ["feature_id"])

    # ── feature_gdt ───────────────────────────────────────────────────────────
    op.create_table(
        "feature_gdt",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("feature_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("tolerance_value", sa.Float(), nullable=False),
        sa.Column("tolerance_zone", sa.String(length=20), nullable=True),
        sa.Column("datum_reference", sa.String(length=50), nullable=True),
        sa.Column("material_condition", sa.String(length=10), nullable=True),
        sa.Column("annotation_position", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["feature_id"], ["drawing_features.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feature_gdt_feature_id", "feature_gdt", ["feature_id"])

    # ── feature_tool_bindings ─────────────────────────────────────────────────
    op.create_table(
        "feature_tool_bindings",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("feature_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("tool_source", tool_source_enum, nullable=False),
        sa.Column("warehouse_item_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("catalog_entry_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("manual_description", sa.Text(), nullable=True),
        sa.Column("cutting_parameters", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("bound_by", sa.String(length=100), nullable=False, server_default="user"),
        sa.ForeignKeyConstraint(["feature_id"], ["drawing_features.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["warehouse_item_id"], ["inventory_items.id"]),
        sa.ForeignKeyConstraint(["catalog_entry_id"], ["tool_catalog_entries.id"]),
        sa.UniqueConstraint("feature_id"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feature_tool_bindings_feature_id", "feature_tool_bindings", ["feature_id"])
    op.create_index("ix_feature_tool_bindings_warehouse_item_id", "feature_tool_bindings", ["warehouse_item_id"])
    op.create_index("ix_feature_tool_bindings_catalog_entry_id", "feature_tool_bindings", ["catalog_entry_id"])


def downgrade() -> None:
    op.drop_table("feature_tool_bindings")
    op.drop_table("feature_gdt")
    op.drop_table("feature_surfaces")
    op.drop_table("feature_dimensions")
    op.drop_table("feature_contours")
    op.drop_table("drawing_features")
    op.drop_table("drawings")
    op.drop_table("tool_catalog_entries")
    op.drop_table("tool_suppliers")

    for enum_name in [
        "drawingstatus", "drawingfeaturetype", "featureprimitivetype",
        "featuredimtype", "roughnesstype", "tooltypeenum", "toolsourceenum",
    ]:
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
