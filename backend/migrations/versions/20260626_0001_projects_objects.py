"""Project / SiteObject tables and document.project_id/object_id FKs.

First-class project & construction-object binding for documents (filter/group
by project/object, beyond the knowledge graph). Idempotent: safe on a clean
create_all baseline and on existing databases.

Revision ID: 20260626_0001
Revises: 20260624_0001
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

from app.db.base import GUID

revision = "20260626_0001"
down_revision = "20260624_0001"
branch_labels = None
depends_on = None


def _has_table(insp, name: str) -> bool:
    return name in insp.get_table_names()


def _has_column(insp, table: str, column: str) -> bool:
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())

    if not _has_table(insp, "projects"):
        op.create_table(
            "projects",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("name", sa.String(300), nullable=False),
            sa.Column("normalized_name", sa.String(300), nullable=False),
            sa.Column("code", sa.String(100), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("normalized_name", name="uq_projects_normalized_name"),
        )
        op.create_index("ix_projects_normalized_name", "projects", ["normalized_name"])
        op.create_index("ix_projects_code", "projects", ["code"])

    if not _has_table(insp, "site_objects"):
        op.create_table(
            "site_objects",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("project_id", GUID(), sa.ForeignKey("projects.id"), nullable=True),
            sa.Column("name", sa.String(300), nullable=False),
            sa.Column("normalized_name", sa.String(300), nullable=False),
            sa.Column("code", sa.String(100), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.UniqueConstraint("normalized_name", name="uq_site_objects_normalized_name"),
        )
        op.create_index("ix_site_objects_normalized_name", "site_objects", ["normalized_name"])
        op.create_index("ix_site_objects_code", "site_objects", ["code"])
        op.create_index("ix_site_objects_project_id", "site_objects", ["project_id"])

    if not _has_column(insp, "documents", "project_id"):
        op.add_column("documents",
                      sa.Column("project_id", GUID(),
                                sa.ForeignKey("projects.id"), nullable=True))
        op.create_index("ix_documents_project_id", "documents", ["project_id"])
    if not _has_column(insp, "documents", "object_id"):
        op.add_column("documents",
                      sa.Column("object_id", GUID(),
                                sa.ForeignKey("site_objects.id"), nullable=True))
        op.create_index("ix_documents_object_id", "documents", ["object_id"])


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if _has_column(insp, "documents", "object_id"):
        op.drop_index("ix_documents_object_id", table_name="documents")
        op.drop_column("documents", "object_id")
    if _has_column(insp, "documents", "project_id"):
        op.drop_index("ix_documents_project_id", table_name="documents")
        op.drop_column("documents", "project_id")
    if _has_table(insp, "site_objects"):
        op.drop_table("site_objects")
    if _has_table(insp, "projects"):
        op.drop_table("projects")
