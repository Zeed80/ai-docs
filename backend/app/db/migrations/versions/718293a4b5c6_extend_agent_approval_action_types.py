"""extend agent approval action types

Revision ID: 718293a4b5c6
Revises: 6a718293a4b5
Create Date: 2026-04-29 17:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "718293a4b5c6"
down_revision: str | None = "6a718293a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_APPROVAL_ACTION_NAMES = (
    "invoice_bulk_delete",
    "warehouse_confirm_receipt",
    "payment_mark_paid",
    "procurement_send_rfq",
    "bom_approve",
    "bom_create_purchase_request",
    "tech_process_plan_approve",
    "tech_norm_estimate_approve",
    "tech_learning_rule_activate",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for action_name in _APPROVAL_ACTION_NAMES:
        op.execute(
            f"ALTER TYPE approvalactiontype ADD VALUE IF NOT EXISTS '{action_name}'"
        )


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely without recreating the type.
    pass
