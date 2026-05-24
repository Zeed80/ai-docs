"""Baseline schema.

Revision ID: 20260424_0001
Revises:
Create Date: 2026-04-24
"""

from __future__ import annotations

from alembic import op

try:
    from app.db.base import Base
    from app.db import models as _models  # noqa: F401  # registers all ORM models
except ImportError:
    from backend.app.db.base import Base  # type: ignore[no-redef]
    from backend.app.db import models as _models  # type: ignore[no-redef] # noqa: F401


revision = "20260424_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
