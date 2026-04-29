"""add_normcard_and_okpd2_fields

Revision ID: db2536766638
Revises: 26c505be7f9c
Create Date: 2026-04-26 16:25:27.310561
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = 'db2536766638'
down_revision: Union[str, None] = '26c505be7f9c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('norm_cards',
    sa.Column('canonical_item_id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('product_code', sa.String(length=100), nullable=True),
    sa.Column('norm_qty', sa.Float(), nullable=False),
    sa.Column('unit', sa.String(length=50), nullable=False),
    sa.Column('loss_factor', sa.Float(), nullable=False),
    sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
    sa.Column('valid_to', sa.DateTime(timezone=True), nullable=True),
    sa.Column('approved_by', sa.String(length=100), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', PG_UUID(as_uuid=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['canonical_item_id'], ['canonical_items.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_norm_cards_canonical_item_id'), 'norm_cards', ['canonical_item_id'], unique=False)
    op.add_column('canonical_items', sa.Column('okpd2_code', sa.String(length=20), nullable=True))
    op.add_column('canonical_items', sa.Column('gost', sa.String(length=200), nullable=True))
    op.add_column('canonical_items', sa.Column('hazard_class', sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column('canonical_items', 'hazard_class')
    op.drop_column('canonical_items', 'gost')
    op.drop_column('canonical_items', 'okpd2_code')
    op.drop_index(op.f('ix_norm_cards_canonical_item_id'), table_name='norm_cards')
    op.drop_table('norm_cards')
