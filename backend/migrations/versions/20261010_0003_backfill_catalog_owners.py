"""E38: owners for supplier catalogs created without one.

Catalogs fetched by URL and archive members were stored without an owner
(the API never passed the requester), and an ownerless document is visible
to everyone together with everything derived from it. Archive members take
the owner and department of their archive; the remaining ownerless catalogs
go to the first active admin, who owns the uploaded documents of the same
installation. The basis is recorded in ``metadata.owner_backfill``.

Attachments from a shared mailbox stay without an owner: a company inbox is
company-wide by design (``_mailbox_owner_sub``), not a legacy gap.

Revision ID: 20261010_0003
Revises: 20261010_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "20261010_0003"
down_revision = "20261010_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            UPDATE documents AS child
               SET owner_sub = parent.owner_sub,
                   department_id = parent.department_id,
                   metadata = (coalesce(child.metadata::jsonb, '{}'::jsonb)
                       || jsonb_build_object('owner_backfill', jsonb_build_object(
                              'basis', 'archive_parent',
                              'revision', '20261010_0003')))::json
              FROM documents AS parent
             WHERE child.owner_sub IS NULL
               AND child.metadata::jsonb ->> 'supplier_catalog' = 'true'
               AND parent.id::text = child.metadata::jsonb ->> 'parent_document_id'
               AND parent.owner_sub IS NOT NULL
            """
        )
    )
    admin = bind.execute(
        sa.text(
            "SELECT sub FROM users WHERE role = 'admin' AND is_active ORDER BY created_at LIMIT 1"
        )
    ).scalar()
    if admin is None:
        return
    bind.execute(
        sa.text(
            """
            UPDATE documents
               SET owner_sub = :admin,
                   metadata = (coalesce(metadata::jsonb, '{}'::jsonb)
                       || jsonb_build_object('owner_backfill', jsonb_build_object(
                              'basis', 'first_active_admin',
                              'revision', '20261010_0003')))::json
             WHERE owner_sub IS NULL
               AND metadata::jsonb ->> 'supplier_catalog' = 'true'
            """
        ),
        {"admin": admin},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE documents
               SET owner_sub = NULL,
                   department_id = CASE
                       WHEN metadata::jsonb -> 'owner_backfill' ->> 'basis' = 'archive_parent'
                       THEN NULL ELSE department_id END,
                   metadata = (metadata::jsonb - 'owner_backfill')::json
             WHERE metadata::jsonb -> 'owner_backfill' ->> 'revision' = '20261010_0003'
            """
        )
    )
