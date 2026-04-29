"""add_quarantine_and_allowlist

Revision ID: a1613cdfff6e
Revises: 604cb1005ed7
Create Date: 2026-04-25
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID


revision: str = 'a1613cdfff6e'
down_revision: Union[str, None] = '604cb1005ed7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ALLOWLIST_DEFAULTS = [
    ".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif",
    ".xlsx", ".xls", ".csv", ".xml", ".json",
]


def upgrade() -> None:
    # Extend documentstatus enum with 'suspicious'
    op.execute("ALTER TYPE documentstatus ADD VALUE IF NOT EXISTS 'suspicious'")

    op.create_table(
        "file_extension_allowlist",
        sa.Column("extension", sa.String(20), nullable=False),
        sa.Column("is_allowed", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("added_by", sa.String(100), nullable=False, server_default="system"),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("extension"),
    )

    op.create_table(
        "quarantine_entries",
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(100), nullable=False),
        sa.Column("original_filename", sa.String(500), nullable=False),
        sa.Column("detected_mime", sa.String(100), nullable=True),
        sa.Column("reviewed_by", sa.String(100), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(20), nullable=True),
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id"),
    )

    # Seed default allowlist
    for ext in ALLOWLIST_DEFAULTS:
        op.execute(
            sa.text(
                "INSERT INTO file_extension_allowlist (id, extension, is_allowed, added_by) "
                "VALUES (gen_random_uuid(), :ext, true, 'system')"
            ).bindparams(ext=ext)
        )


def downgrade() -> None:
    op.drop_table("quarantine_entries")
    op.drop_table("file_extension_allowlist")
