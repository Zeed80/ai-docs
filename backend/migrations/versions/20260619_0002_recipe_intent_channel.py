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
    op.add_column("recipe_skills", sa.Column("intent", sa.String(length=40), nullable=True))
    op.add_column(
        "recipe_skills", sa.Column("output_channel", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("recipe_skills", "output_channel")
    op.drop_column("recipe_skills", "intent")
