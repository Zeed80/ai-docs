"""add technology learning rules

Revision ID: 1b2c3d4e5f60
Revises: 0a1b2c3d4e5f
Create Date: 2026-04-28 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = "1b2c3d4e5f60"
down_revision: Union[str, None] = "0a1b2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "technology_learning_rules",
        sa.Column("rule_type", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("field_name", sa.String(length=120), nullable=False),
        sa.Column("match_old_value", sa.Text(), nullable=True),
        sa.Column("replacement_value", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("suggested_by", sa.String(length=100), nullable=False),
        sa.Column("activated_by", sa.String(length=100), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_technology_learning_rules_rule_type"), "technology_learning_rules", ["rule_type"])
    op.create_index(op.f("ix_technology_learning_rules_entity_type"), "technology_learning_rules", ["entity_type"])
    op.create_index(op.f("ix_technology_learning_rules_field_name"), "technology_learning_rules", ["field_name"])
    op.create_index(op.f("ix_technology_learning_rules_status"), "technology_learning_rules", ["status"])


def downgrade() -> None:
    op.drop_index(op.f("ix_technology_learning_rules_status"), table_name="technology_learning_rules")
    op.drop_index(op.f("ix_technology_learning_rules_field_name"), table_name="technology_learning_rules")
    op.drop_index(op.f("ix_technology_learning_rules_entity_type"), table_name="technology_learning_rules")
    op.drop_index(op.f("ix_technology_learning_rules_rule_type"), table_name="technology_learning_rules")
    op.drop_table("technology_learning_rules")
