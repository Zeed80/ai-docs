"""add_purchase_request_supplier_contract_payment_schedule

Revision ID: 26c505be7f9c
Revises: 53cbfc732ca8
Create Date: 2026-04-26 16:08:56.671245
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = '26c505be7f9c'
down_revision: Union[str, None] = '53cbfc732ca8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('purchase_requests',
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('requested_by', sa.String(length=100), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('items', sa.JSON(), nullable=False),
    sa.Column('deadline', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('compare_session_id', PG_UUID(as_uuid=True), nullable=True),
    sa.Column('approval_id', PG_UUID(as_uuid=True), nullable=True),
    sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['approval_id'], ['approvals.id'], ),
    sa.ForeignKeyConstraint(['compare_session_id'], ['compare_sessions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('supplier_contracts',
    sa.Column('supplier_id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('document_id', PG_UUID(as_uuid=True), nullable=True),
    sa.Column('contract_number', sa.String(length=100), nullable=True),
    sa.Column('start_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('end_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('payment_terms', sa.String(length=200), nullable=True),
    sa.Column('delivery_terms', sa.String(length=200), nullable=True),
    sa.Column('credit_limit', sa.Float(), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
    sa.ForeignKeyConstraint(['supplier_id'], ['parties.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_supplier_contracts_supplier_id'), 'supplier_contracts', ['supplier_id'], unique=False)
    op.create_table('payment_schedules',
    sa.Column('invoice_id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('payment_number', sa.Integer(), nullable=False),
    sa.Column('due_date', sa.DateTime(timezone=True), nullable=False),
    sa.Column('amount', sa.Float(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('payment_method', sa.String(length=50), nullable=True),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('paid_amount', sa.Float(), nullable=True),
    sa.Column('reference', sa.String(length=200), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_payment_schedules_due_date'), 'payment_schedules', ['due_date'], unique=False)
    op.create_index(op.f('ix_payment_schedules_invoice_id'), 'payment_schedules', ['invoice_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_payment_schedules_invoice_id'), table_name='payment_schedules')
    op.drop_index(op.f('ix_payment_schedules_due_date'), table_name='payment_schedules')
    op.drop_table('payment_schedules')
    op.drop_index(op.f('ix_supplier_contracts_supplier_id'), table_name='supplier_contracts')
    op.drop_table('supplier_contracts')
    op.drop_table('purchase_requests')
