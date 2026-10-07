"""Full SQL read access for every model — the operator's explicit decision.

Default (flag off): the agent SQL pipeline sees seven tables and runs under
``agent_sql_reader``, and only local providers (Ollama/Strata) may write the
query inside durable work. With ``sql_full_access`` on, every model, cloud
ones included, sees the schema of every table except the secret ones and the
query runs under ``agent_sql_reader_full``, whose SELECT grants are computed
here from the live schema. The query stays read-only either way.

Secrets stay out in both modes: tables holding provider keys, mailbox
passwords and OAuth tokens, API key hashes, unlock credentials, push topics,
approval callback tokens and the agent's own stored config are not granted,
and a chat's share token is withheld column by column.
"""

from __future__ import annotations

import re

import structlog

logger = structlog.get_logger()

FULL_READER_ROLE = "agent_sql_reader_full"

# Whole tables whose rows are credentials or would hand one out.
SECRET_TABLES = frozenset(
    {
        "agent_config_store",  # MCP server env and other saved agent secrets
        "api_keys",
        "device_registrations",  # ntfy topics: knowing one is being able to push
        "device_unlock_credentials",
        "mail_server_config",
        "mailbox_configs",
        "oauth_app_configs",
        "provider_instances",
        "telegram_approval_callbacks",
    }
)
# Tables that are useful data apart from one secret column.
SECRET_COLUMNS: dict[str, frozenset[str]] = {
    "chat_sessions": frozenset({"share_token"}),
}
# Never offered to the model: migration bookkeeping, not business data.
_HIDDEN_TABLES = frozenset({"alembic_version"})

_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def sql_full_access_enabled() -> bool:
    """The operator's switch; any doubt reads as off."""
    try:
        from app.ai.agent_config import get_builtin_agent_config

        return bool(get_builtin_agent_config().sql_full_access)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("sql_full_access_unreadable", error=str(exc)[:200])
        return False


def readable_tables(all_tables: set[str]) -> set[str]:
    return {t for t in all_tables if t not in SECRET_TABLES and t not in _HIDDEN_TABLES}


async def public_tables(db) -> dict[str, list[tuple[str, str]]]:
    """Every base table of ``public`` with its (column, type) pairs, in order."""
    from sqlalchemy import text

    rows = (
        await db.execute(
            text(
                "SELECT c.table_name, c.column_name, c.data_type "
                "FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                "ORDER BY c.table_name, c.ordinal_position"
            )
        )
    ).all()
    tables: dict[str, list[tuple[str, str]]] = {}
    for table, column, data_type in rows:
        tables.setdefault(table, []).append((column, data_type))
    return tables


async def sync_full_reader_grants(db) -> int:
    """Grant the full reader SELECT on every non-secret table; return the count.

    Revoke-then-grant, so a table that became secret loses its grant and a
    table added by a later migration gains one. Runs on enable and at startup
    while the switch is on.
    """
    from sqlalchemy import text

    role = FULL_READER_ROLE
    await db.execute(
        text(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
            f"CREATE ROLE {role} NOLOGIN; END IF; END $$;"
        )
    )
    await db.execute(text(f"GRANT {role} TO CURRENT_USER"))
    await db.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
    await db.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}"))
    tables = await public_tables(db)
    granted = 0
    for table in sorted(readable_tables(set(tables))):
        if not _IDENT_RE.match(table):
            continue
        hidden = SECRET_COLUMNS.get(table)
        if hidden:
            columns = [c for c, _ in tables[table] if c not in hidden and _IDENT_RE.match(c)]
            if not columns:
                continue
            cols = ", ".join(f'"{c}"' for c in columns)
            await db.execute(text(f'GRANT SELECT ({cols}) ON public."{table}" TO {role}'))
        else:
            await db.execute(text(f'GRANT SELECT ON public."{table}" TO {role}'))
        granted += 1
    logger.info("sql_full_reader_grants_synced", tables=granted)
    return granted


async def revoke_full_reader_grants(db) -> None:
    """Switch off: the full reader keeps no table grant at all."""
    from sqlalchemy import text

    exists = await db.scalar(
        text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": FULL_READER_ROLE}
    )
    if exists:
        await db.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {FULL_READER_ROLE}"))
    logger.info("sql_full_reader_grants_revoked")
