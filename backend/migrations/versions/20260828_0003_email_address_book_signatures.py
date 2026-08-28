"""email address book, signatures, auto-send policy, trigram search

Revision ID: 20260828_0003
Revises: 20260828_0002
Create Date: 2026-08-28
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "20260828_0003"
down_revision: Union[str, None] = "20260828_0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_contacts",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(300), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("organization", sa.String(300), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("owner_sub", sa.String(255), nullable=True),
        sa.Column("party_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("use_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["party_id"], ["parties.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", "owner_sub", name="uq_email_contact_email_owner"),
    )
    op.create_index("ix_email_contacts_email", "email_contacts", ["email"])
    op.create_index("ix_email_contacts_owner_sub", "email_contacts", ["owner_sub"])

    op.create_table(
        "email_signatures",
        sa.Column("id", PG_UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=False),
        sa.Column("owner_sub", sa.String(255), nullable=True),
        sa.Column("mailbox", sa.String(100), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_signatures_owner_sub", "email_signatures", ["owner_sub"])
    op.create_index("ix_email_signatures_mailbox", "email_signatures", ["mailbox"])

    op.add_column("email_rules", sa.Column("auto_send", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.add_column("mail_server_config", sa.Column("auto_send_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("mail_server_config", sa.Column("auto_send_max_per_day", sa.Integer(), nullable=False, server_default="20"))
    op.add_column("mail_server_config", sa.Column("attachment_retention_days", sa.Integer(), nullable=False, server_default="180"))

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        op.execute(sa.text(
            "CREATE INDEX ix_email_messages_subject_trgm ON email_messages "
            "USING GIN (coalesce(subject,'') gin_trgm_ops)"
        ))
        op.execute(sa.text(
            "CREATE INDEX ix_email_messages_body_trgm ON email_messages "
            "USING GIN (coalesce(body_text,'') gin_trgm_ops)"
        ))
        op.execute(sa.text(
            "CREATE INDEX ix_email_contacts_email_trgm ON email_contacts "
            "USING GIN (email gin_trgm_ops)"
        ))
        op.execute(sa.text(
            "CREATE INDEX ix_email_contacts_name_trgm ON email_contacts "
            "USING GIN (coalesce(name,'') gin_trgm_ops)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for ix in (
            "ix_email_contacts_name_trgm", "ix_email_contacts_email_trgm",
            "ix_email_messages_body_trgm", "ix_email_messages_subject_trgm",
        ):
            op.execute(sa.text(f"DROP INDEX IF EXISTS {ix}"))

    op.drop_column("mail_server_config", "attachment_retention_days")
    op.drop_column("mail_server_config", "auto_send_max_per_day")
    op.drop_column("mail_server_config", "auto_send_enabled")
    op.drop_column("email_rules", "auto_send")
    op.drop_table("email_signatures")
    op.drop_table("email_contacts")
