"""Backfill versioned WorkArtifact descriptors for reviewed action receipts."""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260922_0002"
down_revision = "20260913_0001"
branch_labels = None
depends_on = None

_ARTIFACT_TYPES = {
    "agent_control.task_propose": ("agent_task", "database:agent_tasks"),
    "warehouse.update_item": ("inventory_item", "database:inventory_items"),
}


def upgrade():
    bind = op.get_bind()
    rows = (
        bind.execute(
            sa.text(
                """
                SELECT r.logical_action_id, r.work_order_id, r.operation,
                       r.artifact_id, r.artifact_revision, r.response_digest,
                       r.receipt_version, r.created_at, r.attempt_id,
                       wa.step_id
                  FROM action_receipts r
                  LEFT JOIN work_step_attempts wa ON wa.id = r.attempt_id
                 WHERE r.operation IN (
                       'agent_control.task_propose', 'warehouse.update_item')
                """
            )
        )
        .mappings()
        .all()
    )
    artifacts = sa.table(
        "work_artifacts",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("work_order_id", UUID(as_uuid=True)),
        sa.column("step_id", UUID(as_uuid=True)),
        sa.column("artifact_type", sa.String()),
        sa.column("name", sa.String()),
        sa.column("uri", sa.Text()),
        sa.column("content_hash", sa.String()),
        sa.column("content_type", sa.String()),
        sa.column("size_bytes", sa.Integer()),
        sa.column("metadata", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    for row in rows:
        artifact_type, evidence_source = _ARTIFACT_TYPES[row.operation]
        bind.execute(
            artifacts.insert().values(
                id=uuid.uuid4(),
                work_order_id=row.work_order_id,
                step_id=row.step_id,
                artifact_type=artifact_type,
                name=f"{artifact_type}:{row.artifact_id}",
                uri=None,
                content_hash=row.response_digest,
                content_type="application/json",
                size_bytes=None,
                metadata={
                    "descriptor_version": 1,
                    "logical_action_id": str(row.logical_action_id),
                    "operation": row.operation,
                    "recipient_artifact_id": row.artifact_id,
                    "artifact_revision": row.artifact_revision,
                    "receipt_version": row.receipt_version,
                    "evidence_source": evidence_source,
                    "migration": revision,
                },
                created_at=row.created_at,
                updated_at=row.created_at,
            )
        )


def downgrade():
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, metadata FROM work_artifacts WHERE artifact_type IN "
            "('agent_task', 'inventory_item')"
        )
    ).mappings()
    ids = [row.id for row in rows if (row.metadata or {}).get("migration") == revision]
    if ids:
        artifacts = sa.table(
            "work_artifacts",
            sa.column("id", UUID(as_uuid=True)),
        )
        bind.execute(artifacts.delete().where(artifacts.c.id.in_(ids)))
