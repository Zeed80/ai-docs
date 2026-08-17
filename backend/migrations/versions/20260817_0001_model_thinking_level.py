"""Add thinking_level to model_runtime_overrides.

Extends the existing on/off reasoning toggle with an optional reasoning-effort
level (low/medium/high) for models whose provider supports it (gpt-oss-style
reasoning_effort, Anthropic extended-thinking budget tiers, ...). Nullable and
additive: existing rows keep NULL (= no level override, on/off-only
behaviour unchanged).

Revision ID: 20260817_0001
Revises: 20260816_0001
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "20260817_0001"
down_revision = "20260816_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_runtime_overrides",
        sa.Column("thinking_level", sa.String(length=10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_runtime_overrides", "thinking_level")
