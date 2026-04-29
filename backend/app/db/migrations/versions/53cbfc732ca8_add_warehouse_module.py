"""add_warehouse_module

Revision ID: 53cbfc732ca8
Revises: b7f3a2c4d891
Create Date: 2026-04-26 10:39:53.646570
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB

revision: str = '53cbfc732ca8'
down_revision: Union[str, None] = 'b7f3a2c4d891'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('inventory_items',
        sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
        sa.Column('canonical_item_id', PG_UUID(as_uuid=True), nullable=True),
        sa.Column('sku', sa.String(100), nullable=True),
        sa.Column('name', sa.String(500), nullable=False),
        sa.Column('unit', sa.String(50), nullable=False),
        sa.Column('current_qty', sa.Float(), nullable=False, server_default='0'),
        sa.Column('min_qty', sa.Float(), nullable=True),
        sa.Column('location', sa.String(200), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['canonical_item_id'], ['canonical_items.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_inventory_items_sku', 'inventory_items', ['sku'], unique=True)

    op.create_table('stock_movements',
        sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
        sa.Column('inventory_item_id', PG_UUID(as_uuid=True), nullable=False),
        sa.Column('movement_type', sa.String(30), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('balance_after', sa.Float(), nullable=False),
        sa.Column('reference_type', sa.String(50), nullable=True),
        sa.Column('reference_id', PG_UUID(as_uuid=True), nullable=True),
        sa.Column('performed_by', sa.String(100), nullable=False),
        sa.Column('performed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_stock_movements_inventory_item_id', 'stock_movements', ['inventory_item_id'])

    op.create_table('warehouse_receipts',
        sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
        sa.Column('invoice_id', PG_UUID(as_uuid=True), nullable=True),
        sa.Column('document_id', PG_UUID(as_uuid=True), nullable=True),
        sa.Column('supplier_id', PG_UUID(as_uuid=True), nullable=True),
        sa.Column('receipt_number', sa.String(100), nullable=True),
        sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('received_by', sa.String(100), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='draft'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id']),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id']),
        sa.ForeignKeyConstraint(['supplier_id'], ['parties.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_warehouse_receipts_receipt_number', 'warehouse_receipts', ['receipt_number'])

    op.create_table('warehouse_receipt_lines',
        sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
        sa.Column('receipt_id', PG_UUID(as_uuid=True), nullable=False),
        sa.Column('inventory_item_id', PG_UUID(as_uuid=True), nullable=True),
        sa.Column('invoice_line_id', PG_UUID(as_uuid=True), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('quantity_expected', sa.Float(), nullable=False),
        sa.Column('quantity_received', sa.Float(), nullable=False, server_default='0'),
        sa.Column('unit', sa.String(50), nullable=False),
        sa.Column('discrepancy_note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['inventory_item_id'], ['inventory_items.id']),
        sa.ForeignKeyConstraint(['invoice_line_id'], ['invoice_lines.id']),
        sa.ForeignKeyConstraint(['receipt_id'], ['warehouse_receipts.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_warehouse_receipt_lines_receipt_id', 'warehouse_receipt_lines', ['receipt_id'])


def downgrade() -> None:
    op.drop_index('ix_warehouse_receipt_lines_receipt_id', table_name='warehouse_receipt_lines')
    op.drop_table('warehouse_receipt_lines')
    op.drop_index('ix_warehouse_receipts_receipt_number', table_name='warehouse_receipts')
    op.drop_table('warehouse_receipts')
    op.drop_index('ix_stock_movements_inventory_item_id', table_name='stock_movements')
    op.drop_table('stock_movements')
    op.drop_index('ix_inventory_items_sku', table_name='inventory_items')
    op.drop_table('inventory_items')
