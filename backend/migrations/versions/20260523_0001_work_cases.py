"""Work Cases — cockpit feature."""

from alembic import op
from sqlalchemy import inspect as sa_inspect
import sqlalchemy as sa

revision = "20260523_0001"
down_revision = "20260522_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())

    if not insp.has_table("work_cases"):
        op.create_table(
            "work_cases",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("customer", sa.String(300), nullable=True),
            sa.Column("task_description", sa.Text(), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="open"),
            sa.Column("created_by", sa.String(100), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_work_cases_status", "work_cases", ["status"])

    if not insp.has_table("case_documents"):
        op.create_table(
            "case_documents",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("case_id", sa.Uuid(), nullable=False),
            sa.Column("document_id", sa.Uuid(), nullable=False),
            sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("added_by", sa.String(100), nullable=True),
            sa.ForeignKeyConstraint(["case_id"], ["work_cases.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("case_id", "document_id", name="uq_case_document"),
        )
        op.create_index("ix_case_documents_case_id", "case_documents", ["case_id"])
        op.create_index("ix_case_documents_document_id", "case_documents", ["document_id"])


def downgrade() -> None:
    op.drop_table("case_documents")
    op.drop_table("work_cases")
