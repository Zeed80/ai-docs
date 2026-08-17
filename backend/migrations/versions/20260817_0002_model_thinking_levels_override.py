"""Add thinking_levels to model_runtime_overrides.

Lets a reasoning-effort-level determination (live differential probe, or
manual curation) apply to ANY model by key — including a
model_registry.yaml-defined entry, which model_catalog_runtime_entries
cannot override (YAML always wins there via setdefault). Nullable JSON:
NULL = never determined via this path (defer to catalog/auto-derivation);
[] = determined, unsupported; ["low",...] = determined, supported.

Revision ID: 20260817_0002
Revises: 20260817_0001
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op

revision = "20260817_0002"
down_revision = "20260817_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_runtime_overrides",
        sa.Column("thinking_levels", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("model_runtime_overrides", "thinking_levels")
