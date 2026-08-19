"""Add durable learning records for completed work orders.

Revision ID: 20260817_0006
Revises: 20260817_0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260817_0006"
down_revision = "20260817_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memory_facts", sa.Column("valid_from", sa.DateTime(timezone=True)))
    op.add_column("memory_facts", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column(
        "memory_facts",
        sa.Column("status", sa.String(30), server_default="active", nullable=False),
    )
    op.add_column("memory_facts", sa.Column("superseded_by_id", sa.Uuid()))
    op.add_column(
        "memory_facts",
        sa.Column("provenance", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
    )
    op.create_foreign_key(
        "fk_memory_facts_superseded_by",
        "memory_facts",
        "memory_facts",
        ["superseded_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    for column in ("expires_at", "status", "superseded_by_id"):
        op.create_index(f"ix_memory_facts_{column}", "memory_facts", [column])

    op.create_table(
        "work_learnings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_order_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending", nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("lessons", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("provenance", sa.JSON(), server_default=sa.text("'{}'::json"), nullable=False),
        sa.Column("memory_fact_id", sa.Uuid()),
        sa.Column("recipe_skill_id", sa.Uuid()),
        sa.Column("extraction_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.JSON()),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["memory_fact_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipe_skill_id"], ["recipe_skills.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_order_id", name="uq_work_learnings_work_order_id"),
    )
    for column in (
        "work_order_id",
        "status",
        "memory_fact_id",
        "recipe_skill_id",
        "processed_at",
    ):
        op.create_index(f"ix_work_learnings_{column}", "work_learnings", [column])
    op.create_index(
        "ix_work_learnings_process", "work_learnings", ["status", "created_at"]
    )
    op.execute(
        """
        INSERT INTO work_learnings (
            id, work_order_id, status, lessons, provenance,
            extraction_attempts, created_at, updated_at
        )
        SELECT gen_random_uuid(), id, 'pending', '[]'::json, '{}'::json, 0, now(), now()
        FROM work_orders
        WHERE status = 'completed'
        ON CONFLICT (work_order_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table("work_learnings")
    op.drop_constraint("fk_memory_facts_superseded_by", "memory_facts", type_="foreignkey")
    for column in ("superseded_by_id", "status", "expires_at"):
        op.drop_index(f"ix_memory_facts_{column}", table_name="memory_facts")
    for column in ("provenance", "superseded_by_id", "status", "expires_at", "valid_from"):
        op.drop_column("memory_facts", column)
