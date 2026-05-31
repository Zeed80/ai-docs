"""Add ownership/visibility columns: documents.owner_sub/department_id, work_cases.department_id."""

import sqlalchemy as sa
from alembic import op

from app.db.base import GUID

revision = "20260531_0002"
down_revision = "20260531_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(op.get_bind())

    doc_cols = {c["name"] for c in insp.get_columns("documents")}
    with op.batch_alter_table("documents") as batch:
        if "owner_sub" not in doc_cols:
            batch.add_column(sa.Column("owner_sub", sa.String(255), nullable=True))
        if "department_id" not in doc_cols:
            batch.add_column(
                sa.Column(
                    "department_id",
                    GUID(),
                    sa.ForeignKey("departments.id", name="fk_documents_department_id"),
                    nullable=True,
                )
            )
    if "owner_sub" not in doc_cols:
        op.create_index("ix_documents_owner_sub", "documents", ["owner_sub"])
    if "department_id" not in doc_cols:
        op.create_index("ix_documents_department_id", "documents", ["department_id"])

    case_cols = {c["name"] for c in insp.get_columns("work_cases")}
    with op.batch_alter_table("work_cases") as batch:
        if "department_id" not in case_cols:
            batch.add_column(
                sa.Column(
                    "department_id",
                    GUID(),
                    sa.ForeignKey("departments.id", name="fk_work_cases_department_id"),
                    nullable=True,
                )
            )
    if "department_id" not in case_cols:
        op.create_index("ix_work_cases_department_id", "work_cases", ["department_id"])


def downgrade() -> None:
    op.drop_index("ix_work_cases_department_id", table_name="work_cases")
    with op.batch_alter_table("work_cases") as batch:
        batch.drop_column("department_id")

    op.drop_index("ix_documents_department_id", table_name="documents")
    op.drop_index("ix_documents_owner_sub", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("department_id")
        batch.drop_column("owner_sub")
