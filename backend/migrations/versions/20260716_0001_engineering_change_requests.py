"""E3: engineering change requests (change management).

Revision ID: 20260716_0001
Revises: 20260712_0005
Create Date: 2026-07-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260716_0001"
down_revision = "20260712_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "engineering_change_requests" in set(sa_inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "engineering_change_requests",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("engineering_project_id", GUID(), sa.ForeignKey("engineering_projects.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="review"),
        sa.Column("affected_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id"), nullable=False),
        sa.Column("impact", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("reviewers", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("signatures", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("supersedes_id", GUID(), sa.ForeignKey("engineering_change_requests.id")),
        sa.Column("applied_revision_id", GUID(), sa.ForeignKey("engineering_revisions.id")),
        sa.Column("created_by", sa.String(255)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_engineering_change_requests_engineering_project_id", "engineering_change_requests", ["engineering_project_id"])
    op.create_index("ix_engineering_change_requests_affected_revision_id", "engineering_change_requests", ["affected_revision_id"])
    op.create_index("ix_engineering_change_requests_status", "engineering_change_requests", ["status"])
    op.create_index("ix_engineering_change_requests_project_number", "engineering_change_requests", ["engineering_project_id", "number"], unique=True)


def downgrade() -> None:
    op.drop_table("engineering_change_requests")
