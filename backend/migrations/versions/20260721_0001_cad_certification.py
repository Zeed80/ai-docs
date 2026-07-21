"""Normalized CAD projection and two-person certification.

Revision ID: 20260721_0001
Revises: 20260716_0003
Create Date: 2026-07-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260721_0001"
down_revision = "20260716_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa_inspect(op.get_bind()).get_table_names())
    if "cad_element_records" not in tables:
        op.create_table(
            "cad_element_records",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("cad_ir_revision_id", GUID(), sa.ForeignKey("cad_ir_revisions.id"), nullable=False),
            sa.Column("element_id", sa.String(255), nullable=False),
            sa.Column("element_type", sa.String(60), nullable=False),
            sa.Column("assurance", sa.String(40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("source_region", sa.JSON()),
            sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"),
        )
        op.create_index("ix_cad_element_records_cad_ir_revision_id", "cad_element_records", ["cad_ir_revision_id"])
        op.create_index("ix_cad_element_records_element_type", "cad_element_records", ["element_type"])
        op.create_index("ix_cad_element_records_assurance", "cad_element_records", ["assurance"])
        op.create_index("ix_cad_element_records_revision_element", "cad_element_records", ["cad_ir_revision_id", "element_id"], unique=True)
    if "cad_relation_records" not in tables:
        op.create_table(
            "cad_relation_records",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("cad_ir_revision_id", GUID(), sa.ForeignKey("cad_ir_revisions.id"), nullable=False),
            sa.Column("relation_id", sa.String(255), nullable=False),
            sa.Column("relation_type", sa.String(60), nullable=False),
            sa.Column("source_element_id", sa.String(255)),
            sa.Column("target_element_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False, server_default="[]"),
        )
        op.create_index("ix_cad_relation_records_cad_ir_revision_id", "cad_relation_records", ["cad_ir_revision_id"])
        op.create_index("ix_cad_relation_records_relation_type", "cad_relation_records", ["relation_type"])
        op.create_index("ix_cad_relation_records_revision_relation", "cad_relation_records", ["cad_ir_revision_id", "relation_id"], unique=True)
    if "cad_certifications" not in tables:
        op.create_table(
            "cad_certifications",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("cad_ir_revision_id", GUID(), sa.ForeignKey("cad_ir_revisions.id"), nullable=False, unique=True),
            sa.Column("profile", sa.String(40), nullable=False, server_default="auto"),
            sa.Column("status", sa.String(40), nullable=False, server_default="draft"),
            sa.Column("verification", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("drafter_approved_by", sa.String(255)),
            sa.Column("drafter_approved_at", sa.DateTime(timezone=True)),
            sa.Column("normcontrol_approved_by", sa.String(255)),
            sa.Column("normcontrol_approved_at", sa.DateTime(timezone=True)),
            sa.Column("manifest_hash", sa.String(64)),
        )
        op.create_index("ix_cad_certifications_cad_ir_revision_id", "cad_certifications", ["cad_ir_revision_id"], unique=True)
        op.create_index("ix_cad_certifications_status", "cad_certifications", ["status"])


def downgrade() -> None:
    op.drop_table("cad_certifications")
    op.drop_table("cad_relation_records")
    op.drop_table("cad_element_records")
