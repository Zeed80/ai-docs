"""Add departments table and organization fields (department_id, manager_sub, title) to users."""

import sqlalchemy as sa
from alembic import op

from app.db.base import GUID

revision = "20260531_0001"
down_revision = "20260525_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "departments" not in tables:
        op.create_table(
            "departments",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("code", sa.String(50), nullable=False),
            sa.Column("parent_id", GUID(), sa.ForeignKey("departments.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
        op.create_index("ix_departments_code", "departments", ["code"], unique=True)
        op.create_index("ix_departments_parent_id", "departments", ["parent_id"])

    user_cols = {c["name"] for c in insp.get_columns("users")}
    # batch_alter_table: plain ALTER on PostgreSQL, copy-and-move on SQLite — keeps the
    # FK on department_id portable across both dialects.
    with op.batch_alter_table("users") as batch:
        if "department_id" not in user_cols:
            batch.add_column(
                sa.Column(
                    "department_id",
                    GUID(),
                    sa.ForeignKey("departments.id", name="fk_users_department_id"),
                    nullable=True,
                )
            )
        if "manager_sub" not in user_cols:
            batch.add_column(sa.Column("manager_sub", sa.String(255), nullable=True))
        if "title" not in user_cols:
            batch.add_column(sa.Column("title", sa.String(150), nullable=True))

    if "department_id" not in user_cols:
        op.create_index("ix_users_department_id", "users", ["department_id"])
    if "manager_sub" not in user_cols:
        op.create_index("ix_users_manager_sub", "users", ["manager_sub"])


def downgrade() -> None:
    op.drop_index("ix_users_manager_sub", table_name="users")
    op.drop_index("ix_users_department_id", table_name="users")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("title")
        batch.drop_column("manager_sub")
        batch.drop_column("department_id")
    op.drop_index("ix_departments_parent_id", table_name="departments")
    op.drop_index("ix_departments_code", table_name="departments")
    op.drop_table("departments")
