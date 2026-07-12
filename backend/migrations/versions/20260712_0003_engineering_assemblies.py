"""Engineering assemblies, instances and mates.

Revision ID: 20260712_0003
Revises: 20260712_0002
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260712_0003"
down_revision = "20260712_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if "engineering_assemblies" not in tables:
        op.create_table(
            "engineering_assemblies",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("engineering_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id"), nullable=False),
            sa.Column("name", sa.String(300), nullable=False), sa.Column("designation", sa.String(160)),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
            sa.UniqueConstraint("engineering_revision_id", "name", name="uq_engineering_assembly_name"),
        )
        op.create_index("ix_engineering_assemblies_engineering_revision_id", "engineering_assemblies", ["engineering_revision_id"])
    if "engineering_assembly_components" not in tables:
        op.create_table(
            "engineering_assembly_components",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("engineering_assembly_id", GUID(), sa.ForeignKey("engineering_assemblies.id"), nullable=False),
            sa.Column("component_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id")),
            sa.Column("instance_key", sa.String(160), nullable=False), sa.Column("designation", sa.String(300), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"), sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("transform", sa.JSON(), nullable=False, server_default="{}"), sa.Column("bounds", sa.JSON()),
            sa.Column("suppressed", sa.Boolean(), nullable=False, server_default=sa.false()), sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"),
            sa.UniqueConstraint("engineering_assembly_id", "instance_key", name="uq_engineering_component_instance"),
        )
        op.create_index("ix_engineering_assembly_components_engineering_assembly_id", "engineering_assembly_components", ["engineering_assembly_id"])
        op.create_index("ix_engineering_assembly_components_component_revision_id", "engineering_assembly_components", ["component_revision_id"])
    if "engineering_assembly_mates" not in tables:
        op.create_table(
            "engineering_assembly_mates",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("engineering_assembly_id", GUID(), sa.ForeignKey("engineering_assemblies.id"), nullable=False),
            sa.Column("mate_type", sa.String(40), nullable=False), sa.Column("first_instance_key", sa.String(160), nullable=False),
            sa.Column("second_instance_key", sa.String(160), nullable=False), sa.Column("parameters", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(30), nullable=False, server_default="valid"),
        )
        op.create_index("ix_engineering_assembly_mates_engineering_assembly_id", "engineering_assembly_mates", ["engineering_assembly_id"])


def downgrade() -> None:
    op.drop_table("engineering_assembly_mates")
    op.drop_table("engineering_assembly_components")
    op.drop_table("engineering_assemblies")
