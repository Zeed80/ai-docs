"""link tool_suppliers to parties

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-05-03 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "8b9c0d1e2f3a"
down_revision: Union[str, None] = "7a8b9c0d1e2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tool_suppliers",
        sa.Column(
            "main_supplier_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("parties.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_tool_suppliers_main_supplier_id",
        "tool_suppliers",
        ["main_supplier_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_suppliers_main_supplier_id", table_name="tool_suppliers")
    op.drop_column("tool_suppliers", "main_supplier_id")
