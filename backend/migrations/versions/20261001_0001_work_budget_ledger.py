"""Add an inert shared budget ledger and reservation journal.

No existing WorkOrder is linked or backfilled here.  E21.2 must reconcile
historic usage before binding a legacy lineage.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20261001_0001"
down_revision = "20260930_0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "work_budget_ledgers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "root_work_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey(
                "work_orders.id",
                name="fk_work_budget_ledgers_root_work_order_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column("max_active_seconds", sa.Numeric(30, 8), nullable=False),
        sa.Column("max_tool_attempts", sa.Numeric(30, 8), nullable=False),
        sa.Column("max_llm_calls", sa.Numeric(30, 8), nullable=False),
        sa.Column("max_replans", sa.Numeric(30, 8), nullable=False),
        sa.Column("max_tokens", sa.Numeric(30, 8), nullable=True),
        sa.Column("max_cost_usd", sa.Numeric(30, 8), nullable=True),
        sa.Column("blocker", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("root_work_order_id", name="uq_work_budget_ledgers_root_work_order_id"),
    )
    op.create_index(
        "ix_work_budget_ledgers_root_work_order_id",
        "work_budget_ledgers",
        ["root_work_order_id"],
        unique=True,
    )
    op.create_index("ix_work_budget_ledgers_owner_key", "work_budget_ledgers", ["owner_key"])
    op.create_table(
        "work_budget_reservations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ledger_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_budget_ledgers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "work_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("operation_key", sa.String(255), nullable=False),
        sa.Column("dimension", sa.String(40), nullable=False),
        sa.Column("reserved_units", sa.Numeric(30, 8), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("binding_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="reserved"),
        sa.Column("actual_units", sa.Numeric(30, 8), nullable=True),
        sa.Column("actual_unknown", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("settlement_digest", sa.String(64), nullable=True),
        sa.Column("blocker", sa.JSON(), nullable=True),
        sa.Column(
            "reserved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("ledger_id", "operation_key", name="uq_work_budget_operation"),
    )
    op.create_index(
        "ix_work_budget_reservations_ledger_id",
        "work_budget_reservations",
        ["ledger_id"],
    )
    op.create_index(
        "ix_work_budget_reservations_work_order_id",
        "work_budget_reservations",
        ["work_order_id"],
    )
    op.create_index("ix_work_budget_reservations_state", "work_budget_reservations", ["state"])
    op.create_index(
        "ix_work_budget_reservations_ledger_dimension",
        "work_budget_reservations",
        ["ledger_id", "dimension"],
    )
    op.add_column("work_orders", sa.Column("budget_ledger_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_work_orders_budget_ledger_id",
        "work_orders",
        "work_budget_ledgers",
        ["budget_ledger_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_work_orders_budget_ledger_id", "work_orders", ["budget_ledger_id"])


def downgrade():
    op.drop_index("ix_work_orders_budget_ledger_id", table_name="work_orders")
    op.drop_constraint("fk_work_orders_budget_ledger_id", "work_orders", type_="foreignkey")
    op.drop_column("work_orders", "budget_ledger_id")
    op.drop_index(
        "ix_work_budget_reservations_ledger_dimension",
        table_name="work_budget_reservations",
    )
    op.drop_index("ix_work_budget_reservations_state", table_name="work_budget_reservations")
    op.drop_index(
        "ix_work_budget_reservations_work_order_id",
        table_name="work_budget_reservations",
    )
    op.drop_index("ix_work_budget_reservations_ledger_id", table_name="work_budget_reservations")
    op.drop_table("work_budget_reservations")
    op.drop_index("ix_work_budget_ledgers_owner_key", table_name="work_budget_ledgers")
    op.drop_index("ix_work_budget_ledgers_root_work_order_id", table_name="work_budget_ledgers")
    op.drop_table("work_budget_ledgers")
