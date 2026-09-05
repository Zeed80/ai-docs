"""catalog pages registry + per-entry images

Makes the PAGE the unit of catalog ingestion: a resumable checkpoint, a real
page number for every position, and a rendered image to crop product pictures
from and to show in the viewer.

Revision ID: 20260822_0001
Revises: 20260821_0002
Create Date: 2026-08-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260822_0001"
down_revision: str | None = "20260821_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "catalog_pages",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("width_pt", sa.Float(), nullable=True),
        sa.Column("height_pt", sa.Float(), nullable=True),
        sa.Column("image_path", sa.String(1000), nullable=True),
        sa.Column("image_width", sa.Integer(), nullable=True),
        sa.Column("image_height", sa.Integer(), nullable=True),
        sa.Column("thumb_path", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("skip_reason", sa.String(50), nullable=True),
        sa.Column("text_source", sa.String(10), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=True),
        sa.Column("entries_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("images", sa.JSON(), nullable=True),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
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
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "page_number", name="uq_catalog_pages_document_page"),
    )
    op.create_index("ix_catalog_pages_document_id", "catalog_pages", ["document_id"])
    op.create_index("ix_catalog_pages_status", "catalog_pages", ["status"])
    op.create_index("ix_catalog_pages_run_id", "catalog_pages", ["run_id"])
    # The batch worker picks work with this exact predicate.
    op.create_index(
        "ix_catalog_pages_doc_status_page",
        "catalog_pages",
        ["document_id", "status", "page_number"],
    )

    for column in (
        sa.Column("catalog_page_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("image_path", sa.String(1000), nullable=True),
        sa.Column("image_thumb_path", sa.String(1000), nullable=True),
        sa.Column("image_bbox", sa.JSON(), nullable=True),
        sa.Column("image_kind", sa.String(20), nullable=True),
        sa.Column("image_confidence", sa.Float(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=True),
    ):
        op.add_column("tool_catalog_entries", column)

    op.create_index(
        "ix_tool_catalog_entries_catalog_page_id", "tool_catalog_entries", ["catalog_page_id"]
    )
    op.create_index("ix_tool_catalog_entries_image_kind", "tool_catalog_entries", ["image_kind"])
    op.create_index(
        "ix_tool_catalog_entries_content_hash", "tool_catalog_entries", ["content_hash"]
    )
    op.create_foreign_key(
        "fk_tool_catalog_entries_catalog_page",
        "tool_catalog_entries",
        "catalog_pages",
        ["catalog_page_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Positions imported before page-wise parsing keep working and stay
    # searchable; the UI groups them under "Без привязки" and offers a re-parse
    # instead of silently pretending they came from a page.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE tool_catalog_entries
                   SET metadata = (
                           COALESCE(metadata::jsonb, '{}'::jsonb)
                           || '{"legacy_import": true}'::jsonb
                       )::json
                 WHERE catalog_page_id IS NULL
                """
            )
        )


def downgrade() -> None:
    op.drop_constraint(
        "fk_tool_catalog_entries_catalog_page", "tool_catalog_entries", type_="foreignkey"
    )
    for name in (
        "ix_tool_catalog_entries_content_hash",
        "ix_tool_catalog_entries_image_kind",
        "ix_tool_catalog_entries_catalog_page_id",
    ):
        op.drop_index(name, "tool_catalog_entries")
    for column in (
        "content_hash",
        "image_confidence",
        "image_kind",
        "image_bbox",
        "image_thumb_path",
        "image_path",
        "catalog_page_id",
    ):
        op.drop_column("tool_catalog_entries", column)
    op.drop_table("catalog_pages")
