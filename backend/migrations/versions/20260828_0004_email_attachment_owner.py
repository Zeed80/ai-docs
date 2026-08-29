"""Ф0.1: staged compose attachments — nullable message_id + uploaded_by_sub.

Two fixes in one revision because they are the same row:

* ``message_id`` was NOT NULL, but ``POST /api/email/attachments/upload``
  deliberately stages a file *before* any message exists and passes
  ``message_id=None``. Attaching a file to an outbound email therefore failed
  with an IntegrityError (HTTP 500) on every attempt — found while adding the
  ownership check below, not covered by any test.
* ``uploaded_by_sub`` records who staged the file, so a draft cannot reference
  an attachment belonging to someone else (app/api/email.py, and again in
  app/tasks/email_sender.py before the bytes go into the MIME message).

Revision ID: 20260828_0004
Revises: 20260828_0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0004"
down_revision: Union[str, None] = "20260828_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("email_attachments")}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)

    if "uploaded_by_sub" not in cols:
        op.add_column(
            "email_attachments",
            sa.Column("uploaded_by_sub", sa.String(255), nullable=True),
        )
        op.create_index(
            "ix_email_attachments_uploaded_by_sub",
            "email_attachments",
            ["uploaded_by_sub"],
        )

    # A staged (not yet sent) attachment has no message.
    op.alter_column(
        "email_attachments", "message_id", existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "uploaded_by_sub" in _columns(bind):
        op.drop_index("ix_email_attachments_uploaded_by_sub", table_name="email_attachments")
        op.drop_column("email_attachments", "uploaded_by_sub")
    # Orphan staged rows would block the NOT NULL restore; drop them first.
    op.execute(sa.text("DELETE FROM email_attachments WHERE message_id IS NULL"))
    op.alter_column(
        "email_attachments", "message_id", existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
