"""Source connectors — learned working strategies for reaching a web source.

Ф5 (AGENT_AUTONOMY_ROADMAP.md): direct structural sibling of recipe_skills
(20260611_0001), scoped to "how to reach this domain/pattern" instead of
"how to do this task". See SourceConnector's own docstring in
backend/app/db/models.py for why this is a separate model rather than an
extension of MemoryFact(kind="web_source").

Revision ID: 20260820_0002
Revises: 20260820_0001
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260820_0002"
down_revision = "20260820_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if not insp.has_table("source_connectors"):
        op.create_table(
            "source_connectors",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("domain_pattern", sa.String(255), nullable=False),
            sa.Column("strategy", sa.JSON(), nullable=False),
            sa.Column("trigger_examples", sa.JSON(), nullable=False),
            sa.Column("schema_hash", sa.String(64), nullable=True),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("revalidate_after", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
            sa.Column("created_by", sa.String(100), nullable=False, server_default="sveta"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_source_connectors_domain_pattern", "source_connectors", ["domain_pattern"])
        op.create_index("ix_source_connectors_status", "source_connectors", ["status"])


def downgrade() -> None:
    op.drop_table("source_connectors")
