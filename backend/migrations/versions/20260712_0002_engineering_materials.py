"""Engineering material catalog and per-revision assignments.

Revision ID: 20260712_0002
Revises: 20260712_0001
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect
from app.db.base import GUID

revision = "20260712_0002"
down_revision = "20260712_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if "engineering_materials" not in tables:
        op.create_table(
        "engineering_materials",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("designation", sa.String(160), nullable=False), sa.Column("standard", sa.String(160)),
        sa.Column("description", sa.Text()), sa.Column("density_kg_m3", sa.Float()),
        sa.Column("elastic_modulus_mpa", sa.Float()), sa.Column("yield_strength_mpa", sa.Float()),
        sa.Column("tensile_strength_mpa", sa.Float()), sa.Column("thermal_expansion_1_k", sa.Float()),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("designation", "standard", name="uq_engineering_material_designation"),
        )
        op.create_index("ix_engineering_materials_designation", "engineering_materials", ["designation"])
    if "engineering_material_assignments" not in tables:
        op.create_table(
        "engineering_material_assignments",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("engineering_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id"), nullable=False),
        sa.Column("material_id", GUID(), sa.ForeignKey("engineering_materials.id"), nullable=False),
        sa.Column("object_key", sa.String(160), nullable=False), sa.Column("source", sa.String(30), nullable=False, server_default="manual"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"), sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("engineering_revision_id", "object_key", name="uq_engineering_material_assignment"),
        )
        op.create_index("ix_engineering_material_assignments_engineering_revision_id", "engineering_material_assignments", ["engineering_revision_id"])
        op.create_index("ix_engineering_material_assignments_material_id", "engineering_material_assignments", ["material_id"])


def downgrade() -> None:
    op.drop_table("engineering_material_assignments")
    op.drop_table("engineering_materials")
