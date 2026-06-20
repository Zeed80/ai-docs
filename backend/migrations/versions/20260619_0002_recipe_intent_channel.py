"""Add intent + output_channel to recipe_skills (TurnDecision reproducibility contract).

Revision ID: 20260619_0002
Revises: 20260619_0001
Create Date: 2026-06-19
"""

from alembic import op
import sqlalchemy as sa

revision = "20260619_0002"
down_revision = "20260619_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: the baseline migration's create_all already builds the current
    # recipe_skills columns on a clean install, so only add what is missing.
    insp = sa.inspect(op.get_bind())
    existing = {c["name"] for c in insp.get_columns("recipe_skills")}
    if "intent" not in existing:
        op.add_column(
            "recipe_skills", sa.Column("intent", sa.String(length=40), nullable=True)
        )
    if "output_channel" not in existing:
        op.add_column(
            "recipe_skills",
            sa.Column("output_channel", sa.String(length=20), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("recipe_skills", "output_channel")
    op.drop_column("recipe_skills", "intent")
