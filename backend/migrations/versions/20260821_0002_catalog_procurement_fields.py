"""catalog ↔ procurement: price source, VAT/pack normalisation, canonical link

Revision ID: 20260821_0002
Revises: 20260821_0001
Create Date: 2026-08-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260821_0002"
down_revision: Union[str, None] = "20260821_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "price_history_entries", sa.Column("source", sa.String(30), nullable=True)
    )
    op.create_index(
        "ix_price_history_entries_source", "price_history_entries", ["source"]
    )

    for name, column in (
        ("unit", sa.Column("unit", sa.String(30), nullable=True)),
        ("price_includes_vat", sa.Column("price_includes_vat", sa.Boolean(), nullable=True)),
        ("vat_rate", sa.Column("vat_rate", sa.Float(), nullable=True)),
        ("pack_size", sa.Column("pack_size", sa.Float(), nullable=True)),
        ("min_order_qty", sa.Column("min_order_qty", sa.Float(), nullable=True)),
        ("valid_from", sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True)),
        ("valid_until", sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True)),
        (
            "canonical_item_id",
            sa.Column("canonical_item_id", PG_UUID(as_uuid=True), nullable=True),
        ),
    ):
        op.add_column("tool_catalog_entries", column)

    op.create_index(
        "ix_tool_catalog_entries_canonical_item_id",
        "tool_catalog_entries",
        ["canonical_item_id"],
    )
    op.create_foreign_key(
        "fk_tool_catalog_entries_canonical_item",
        "tool_catalog_entries",
        "canonical_items",
        ["canonical_item_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tool_catalog_entries_canonical_item", "tool_catalog_entries", type_="foreignkey"
    )
    op.drop_index("ix_tool_catalog_entries_canonical_item_id", "tool_catalog_entries")
    for name in (
        "canonical_item_id", "valid_until", "valid_from", "min_order_qty",
        "pack_size", "vat_rate", "price_includes_vat", "unit",
    ):
        op.drop_column("tool_catalog_entries", name)
    op.drop_index("ix_price_history_entries_source", "price_history_entries")
    op.drop_column("price_history_entries", "source")
