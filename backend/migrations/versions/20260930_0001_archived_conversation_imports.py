"""Store inert, owner-selected legacy chat context."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260930_0001"
down_revision = "20260929_0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "archived_conversation_imports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column("request_id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column(
            "source_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_session_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_sessions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("records", sa.JSON(), nullable=False),
        sa.Column("attachment_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_key", "request_id", name="uq_archive_import_owner_request"),
        sa.UniqueConstraint("target_session_id", name="uq_archive_import_target_session"),
    )
    op.create_index(
        "ix_archived_conversation_imports_owner_key",
        "archived_conversation_imports",
        ["owner_key"],
    )
    op.create_index(
        "ix_archive_import_source_session",
        "archived_conversation_imports",
        ["source_session_id"],
    )


def downgrade():
    op.drop_index("ix_archive_import_source_session", table_name="archived_conversation_imports")
    op.drop_index(
        "ix_archived_conversation_imports_owner_key",
        table_name="archived_conversation_imports",
    )
    op.drop_table("archived_conversation_imports")
