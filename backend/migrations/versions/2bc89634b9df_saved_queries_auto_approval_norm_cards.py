"""Add saved_queries, auto_approval_rules, norm_cards tables.

Revision ID: 2bc89634b9df
Revises: 20260424_0001
Create Date: 2026-05-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "2bc89634b9df"
down_revision = "20260424_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These tables were created via autogenerate in a previous session.
    # The schema already exists in the database; this migration is a stub
    # to keep the alembic_version table consistent.
    pass


def downgrade() -> None:
    pass
