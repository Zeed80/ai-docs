"""Detect tables/columns the ORM expects but the live schema lacks.

``alembic_version`` only proves which revision ids ran, not what the schema
holds: a revision edited after it was applied is never replayed. On
2026-10-05 production sat at head without ``durable_chat_runs.source_binding_id``
and every chat submit returned HTTP 500 for a week. Only missing tables and
columns are reported; index differences are noise here because many indexes
are created by migrations without being declared on the models.
"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import MetaData
from sqlalchemy.engine import Connection


def missing_schema_objects(connection: Connection, metadata: MetaData) -> list[str]:
    """Return ``table`` / ``table.column`` names absent from the live schema."""
    context = MigrationContext.configure(connection, opts={"compare_type": False})
    missing: list[str] = []
    for diff in compare_metadata(context, metadata):
        if isinstance(diff, list):
            continue  # modify_* entries come wrapped in a list
        kind = diff[0]
        if kind == "add_table":
            missing.append(diff[1].name)
        elif kind == "add_column":
            missing.append(f"{diff[2]}.{diff[3].name}")
    return sorted(missing)
