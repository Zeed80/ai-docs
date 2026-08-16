"""Preserve the pre-discount line total on invoice_lines.

Some Russian invoices have a separate "Скидка" (discount) column: "Сумма без
скидки" | "Скидка" | "Сумма". Extraction already correctly uses the final
post-discount "Сумма" for `amount` (see extraction_prompts.py), but the raw
pre-discount figure had nowhere to go and was silently dropped, so the
discount itself was invisible to review/reporting even though the totals
were arithmetically correct.

Revision ID: 20260816_0001
Revises: 20260809_0002
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op

revision = "20260816_0001"
down_revision = "20260809_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("invoice_lines", sa.Column("pre_discount_amount", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("invoice_lines", "pre_discount_amount")
