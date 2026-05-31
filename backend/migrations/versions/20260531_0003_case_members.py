"""Add case_members table for multi-user collaboration on work cases."""

import sqlalchemy as sa
from alembic import op

from app.db.base import GUID

revision = "20260531_0003"
down_revision = "20260531_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(op.get_bind())
    if "case_members" in set(insp.get_table_names()):
        return

    op.create_table(
        "case_members",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("case_id", GUID(), sa.ForeignKey("work_cases.id"), nullable=False),
        sa.Column("user_sub", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="collaborator"),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("added_by", sa.String(255), nullable=True),
        sa.UniqueConstraint("case_id", "user_sub", name="uq_case_member"),
    )
    op.create_index("ix_case_members_case_id", "case_members", ["case_id"])
    op.create_index("ix_case_members_user_sub", "case_members", ["user_sub"])


def downgrade() -> None:
    op.drop_index("ix_case_members_user_sub", table_name="case_members")
    op.drop_index("ix_case_members_case_id", table_name="case_members")
    op.drop_table("case_members")
