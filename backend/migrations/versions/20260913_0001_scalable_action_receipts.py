"""Add one immutable, uniquely keyed receipt per logical action.

Only self-consistent pilot WorkEvent receipts are copied. Ambiguous, corrupt,
or duplicate events remain in the event log for quarantine and investigation;
their hashes are never repaired during migration.
"""

import hashlib
import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260913_0001"
down_revision = "20260912_0001"
branch_labels = None
depends_on = None


def _digest(value):
    canonical = json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


def _valid_candidate(row):
    payload = _json(row.payload)
    request = _json(row.request)
    if not isinstance(payload, dict) or not isinstance(request, dict):
        return None
    try:
        logical_action_id = uuid.UUID(str(payload["action_id"]))
        attempt_id = uuid.UUID(str(payload["attempt_id"]))
        response = _json(payload["response"])
        artifact_id = str(uuid.UUID(str(response["id"])))
        artifact_revision = response["updated_at"]
        arguments = _json(request["arguments"])
    except (ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
        return None
    if (
        logical_action_id != row.logical_action_id
        or not isinstance(response, dict)
        or not isinstance(artifact_revision, str)
        or not artifact_revision
        or not isinstance(arguments, dict)
        or request.get("name") != "agent_control"
        or arguments.get("action") != "task_propose"
        or payload.get("operation") != "agent_control.task_propose"
        or payload.get("request_digest") != row.action_request_digest
        or _digest(request) != row.action_request_digest
        or payload.get("response_digest") != _digest(response)
        or payload.get("evidence_scope") != "database_commit"
        or payload.get("can_replay") is not False
        or row.event_step_id is None
        or attempt_id != row.event_attempt_id
        # A changed current fence makes the historical event ambiguous. Keep
        # it in WorkEvent for backward read instead of guessing ownership.
        or attempt_id != row.action_attempt_id
    ):
        return None
    return {
        "logical_action_id": logical_action_id,
        "attempt_id": attempt_id,
        "response": response,
        "artifact_id": artifact_id,
        "artifact_revision": artifact_revision,
        "payload": payload,
    }


def _backfill_pilot_receipts(bind):
    rows = (
        bind.execute(
            sa.text(
                """
            SELECT e.id AS event_id, e.work_order_id, e.sequence, e.payload, e.created_at,
                   a.id AS logical_action_id, a.attempt_id AS action_attempt_id,
                   a.request, a.request_digest AS action_request_digest,
                   o.owner_key, wa.id AS event_attempt_id, ws.id AS event_step_id
              FROM work_events e
              JOIN chat_logical_actions a
                ON a.id = CASE
                     WHEN (e.payload ->> 'action_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                     THEN (e.payload ->> 'action_id')::uuid
                     ELSE NULL
                   END
               AND a.work_order_id = e.work_order_id
              JOIN work_orders o ON o.id = e.work_order_id
              LEFT JOIN work_step_attempts wa
                ON wa.id = CASE
                     WHEN (e.payload ->> 'attempt_id') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                     THEN (e.payload ->> 'attempt_id')::uuid
                     ELSE NULL
                   END
              LEFT JOIN work_steps ws
                ON ws.id = wa.step_id AND ws.work_order_id = e.work_order_id
             WHERE e.event_type = 'chat.recipient_committed'
             ORDER BY e.sequence, e.id
            """
            )
        )
        .mappings()
        .all()
    )

    grouped = {}
    for row in rows:
        grouped.setdefault(row.logical_action_id, []).append(row)

    receipts = sa.table(
        "action_receipts",
        sa.column("id", UUID(as_uuid=True)),
        sa.column("logical_action_id", UUID(as_uuid=True)),
        sa.column("work_order_id", UUID(as_uuid=True)),
        sa.column("owner_key", sa.String()),
        sa.column("attempt_id", UUID(as_uuid=True)),
        sa.column("operation", sa.String()),
        sa.column("request_digest", sa.String()),
        sa.column("response", sa.JSON()),
        sa.column("response_digest", sa.String()),
        sa.column("artifact_id", sa.String()),
        sa.column("artifact_revision", sa.String()),
        sa.column("receipt_version", sa.SmallInteger()),
        sa.column("provenance", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for logical_action_id, candidates in grouped.items():
        if len(candidates) != 1:
            continue
        row = candidates[0]
        valid = _valid_candidate(row)
        if valid is None:
            continue
        payload = valid["payload"]
        bind.execute(
            receipts.insert().values(
                id=uuid.uuid4(),
                logical_action_id=logical_action_id,
                work_order_id=row.work_order_id,
                owner_key=row.owner_key,
                attempt_id=valid["attempt_id"],
                operation=payload["operation"],
                request_digest=payload["request_digest"],
                response=valid["response"],
                response_digest=payload["response_digest"],
                artifact_id=valid["artifact_id"],
                artifact_revision=valid["artifact_revision"],
                receipt_version=1,
                provenance={
                    "source": "work_event",
                    "event_id": str(row.event_id),
                    "event_sequence": row.sequence,
                    "legacy_receipt_version": 0,
                    "migration": revision,
                },
                created_at=row.created_at,
            )
        )


def upgrade():
    op.create_table(
        "action_receipts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "logical_action_id",
            UUID(as_uuid=True),
            sa.ForeignKey("chat_logical_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "work_order_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_key", sa.String(200), nullable=False),
        sa.Column(
            "attempt_id",
            UUID(as_uuid=True),
            sa.ForeignKey("work_step_attempts.id"),
            nullable=False,
        ),
        sa.Column("operation", sa.String(200), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("response_digest", sa.String(64), nullable=False),
        sa.Column("artifact_id", sa.String(300), nullable=False),
        sa.Column("artifact_revision", sa.String(300), nullable=False),
        sa.Column("receipt_version", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("logical_action_id", name="uq_action_receipts_logical_action"),
    )
    op.create_index(
        "ix_action_receipts_order_operation",
        "action_receipts",
        ["work_order_id", "operation"],
    )
    op.create_index("ix_action_receipts_owner", "action_receipts", ["owner_key"])
    _backfill_pilot_receipts(op.get_bind())


def downgrade():
    op.drop_table("action_receipts")
