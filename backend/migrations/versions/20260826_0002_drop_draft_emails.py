"""drop the dead draft_emails table

Two parallel outbound-mail pipelines existed: DraftAction (real SMTP send via
app/tasks/email_sender.py) and this one. The second looked complete — model,
/api/draft-emails router, risk flags, an approval_id column — but its approval
path never dispatched a send, so procurement RFQs and payment follow-ups sat in
it forever. 20260826_0001's commit moved both callers onto DraftAction; this
removes what is now unreachable: the router, the ORM models (both the
app/db/models.py and the legacy app/domain/models.py copies), the dead
services.py helpers, and the table itself.

Verified empty in production before writing this (0 rows, no non-draft rows),
so no data migration is needed — but downgrade() recreates the structure rather
than pretending the drop is free.

Revision ID: 20260826_0002
Revises: 20260826_0001
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision: str = "20260826_0002"
down_revision: str | None = "20260826_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Guarded: the baseline builds the schema with create_all, so a fresh
    # install may or may not have reached this table by the time we run.
    if sa_inspect(op.get_bind()).has_table("draft_emails"):
        op.drop_table("draft_emails")


def downgrade() -> None:
    op.create_table(
        "draft_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
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
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("related_entity_type", sa.String(50), nullable=True),
        sa.Column("related_entity_id", sa.Uuid(), nullable=True),
        sa.Column("to_addresses", sa.JSON(), nullable=False),
        sa.Column("cc_addresses", sa.JSON(), nullable=True),
        sa.Column("subject", sa.String(1000), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("risk_flags", sa.JSON(), nullable=True),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("generated_by", sa.String(50), nullable=True, server_default="sveta"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["thread_id"], ["email_threads.id"]),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
