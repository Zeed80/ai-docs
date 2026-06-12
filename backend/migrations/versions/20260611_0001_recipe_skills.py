"""Recipe skills — learned declarative macros (self-learning without codegen).

Revision ID: 20260611_0001
Revises: 20260608_0001
Create Date: 2026-06-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260611_0001"
down_revision = "20260608_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    if not insp.has_table("recipe_skills"):
        op.create_table(
            "recipe_skills",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("role", sa.String(80), nullable=False, server_default="data_analyst"),
            sa.Column("trigger_examples", sa.JSON(), nullable=False),
            sa.Column("steps", sa.JSON(), nullable=False),
            sa.Column("param_slots", sa.JSON(), nullable=True),
            sa.Column("source_session_id", sa.String(64), nullable=True),
            sa.Column("capability_schema_hash", sa.String(64), nullable=True),
            sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
        op.create_index("ix_recipe_skills_name", "recipe_skills", ["name"])
        op.create_index("ix_recipe_skills_status", "recipe_skills", ["status"])


def downgrade() -> None:
    op.drop_table("recipe_skills")
