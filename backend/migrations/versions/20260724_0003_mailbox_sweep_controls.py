"""Mailbox sweep controls: opt-in triage, UID-based progress, quota.

Adds to ``mailbox_configs``:
  * ``sweep_enabled`` — may the agent read this mailbox at all. Shared/integration
    mailboxes keep the old behaviour (true); personal mailboxes default to FALSE —
    reading an employee's private mail is an explicit, revocable consent, not a
    side effect of provisioning.
  * ``last_seen_uid`` — IMAP UID watermark, so a personal mailbox can be polled
    with BODY.PEEK (no \\Seen flag) without re-ingesting everything each run.
  * ``quota_mb`` — the quota the mailbox was provisioned with (Mailcow side),
    shown/edited in the admin UI instead of the previously hardcoded 1024.

Adds ``mail_server_config.default_quota_mb`` — the default applied to new
mailboxes when the admin does not override it.

Revision ID: 20260724_0003
Revises: 20260724_0002
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect

revision = "20260724_0003"
down_revision = "20260724_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa_inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "mail_server_config" in tables:
        cfg_columns = {c["name"] for c in insp.get_columns("mail_server_config")}
        if "default_quota_mb" not in cfg_columns:
            op.add_column(
                "mail_server_config",
                sa.Column("default_quota_mb", sa.Integer(), nullable=False, server_default="1024"),
            )

    if "mailbox_configs" not in tables:
        return
    columns = {c["name"] for c in insp.get_columns("mailbox_configs")}

    if "sweep_enabled" not in columns:
        op.add_column(
            "mailbox_configs",
            sa.Column("sweep_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        # Existing personal mailboxes (provisioned before this migration) were
        # being swept without consent — turn them off, let their owners opt in.
        if "mailbox_type" in columns:
            op.execute(
                "UPDATE mailbox_configs SET sweep_enabled = false "
                "WHERE mailbox_type = 'personal'"
            )

    if "last_seen_uid" not in columns:
        op.add_column("mailbox_configs", sa.Column("last_seen_uid", sa.Integer(), nullable=True))

    if "quota_mb" not in columns:
        op.add_column(
            "mailbox_configs",
            sa.Column("quota_mb", sa.Integer(), nullable=False, server_default="1024"),
        )


def downgrade() -> None:
    insp = sa_inspect(op.get_bind())
    tables = set(insp.get_table_names())

    if "mail_server_config" in tables:
        cfg_columns = {c["name"] for c in insp.get_columns("mail_server_config")}
        if "default_quota_mb" in cfg_columns:
            op.drop_column("mail_server_config", "default_quota_mb")

    if "mailbox_configs" not in tables:
        return
    columns = {c["name"] for c in insp.get_columns("mailbox_configs")}
    for name in ("quota_mb", "last_seen_uid", "sweep_enabled"):
        if name in columns:
            op.drop_column("mailbox_configs", name)
