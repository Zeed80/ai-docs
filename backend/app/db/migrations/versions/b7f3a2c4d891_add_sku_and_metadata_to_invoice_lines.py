"""add_sku_and_metadata_to_invoice_lines

Revision ID: b7f3a2c4d891
Revises: a1613cdfff6e
Create Date: 2026-04-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = 'b7f3a2c4d891'
down_revision: Union[str, None] = 'a1613cdfff6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('invoice_lines', sa.Column('sku', sa.String(200), nullable=True))
    op.add_column('invoice_lines', sa.Column('weight', sa.Float(), nullable=True))
    op.add_column('invoice_lines', sa.Column('metadata_', JSONB(), nullable=True))
    op.create_index('ix_invoice_lines_sku', 'invoice_lines', ['sku'])

    # Store extra extracted invoice fields (payment_id, notes, validity_date)
    op.add_column('invoices', sa.Column('payment_id', sa.String(500), nullable=True))
    op.add_column('invoices', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('invoices', sa.Column('validity_date', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_index('ix_invoice_lines_sku', table_name='invoice_lines')
    op.drop_column('invoice_lines', 'sku')
    op.drop_column('invoice_lines', 'weight')
    op.drop_column('invoice_lines', 'metadata_')
    op.drop_column('invoices', 'payment_id')
    op.drop_column('invoices', 'notes')
    op.drop_column('invoices', 'validity_date')
