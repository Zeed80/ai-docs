"""add_bom

Revision ID: 5c09365341d1
Revises: db2536766638
Create Date: 2026-04-26 16:30:54.649856
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = '5c09365341d1'
down_revision: Union[str, None] = 'db2536766638'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('boms',
    sa.Column('product_name', sa.String(length=500), nullable=False),
    sa.Column('product_code', sa.String(length=100), nullable=True),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('document_id', PG_UUID(as_uuid=True), nullable=True),
    sa.Column('approved_by', sa.String(length=100), nullable=True),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('product_code')
    )
    op.create_table('bom_lines',
    sa.Column('bom_id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('line_number', sa.Integer(), nullable=False),
    sa.Column('canonical_item_id', PG_UUID(as_uuid=True), nullable=True),
    sa.Column('norm_card_id', PG_UUID(as_uuid=True), nullable=True),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('unit', sa.String(length=50), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
    sa.ForeignKeyConstraint(['bom_id'], ['boms.id'], ),
    sa.ForeignKeyConstraint(['canonical_item_id'], ['canonical_items.id'], ),
    sa.ForeignKeyConstraint(['norm_card_id'], ['norm_cards.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_bom_lines_bom_id'), 'bom_lines', ['bom_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_bom_lines_bom_id'), table_name='bom_lines')
    op.drop_table('bom_lines')
    op.drop_table('boms')
