"""Database-aware text search helpers."""

from __future__ import annotations

from sqlalchemy import String, cast, func, literal_column, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement


def is_postgresql_session(db: AsyncSession) -> bool:
    bind = db.get_bind()
    return bool(bind and bind.dialect.name == "postgresql")


def ilike_condition(columns: list[ColumnElement], query: str) -> ColumnElement:
    pattern = f"%{query}%"
    return or_(*(cast(column, String).ilike(pattern) for column in columns))


def text_search_condition(
    db: AsyncSession,
    columns: list[ColumnElement],
    query: str,
    *,
    config: str = "russian",
) -> ColumnElement:
    if not is_postgresql_session(db):
        return ilike_condition(columns, query)
    return or_(postgres_fts_condition(columns, query, config=config), ilike_condition(columns, query))


def text_search_rank(
    db: AsyncSession,
    columns: list[ColumnElement],
    query: str,
    *,
    config: str = "russian",
) -> ColumnElement | None:
    if not is_postgresql_session(db):
        return None
    pg_config = _postgres_config_literal(config)
    return func.ts_rank_cd(
        func.to_tsvector(pg_config, _concat_columns(columns)),
        func.plainto_tsquery(pg_config, query),
    )


def postgres_fts_condition(
    columns: list[ColumnElement],
    query: str,
    *,
    config: str = "russian",
) -> ColumnElement:
    pg_config = _postgres_config_literal(config)
    return func.to_tsvector(pg_config, _concat_columns(columns)).op("@@")(
        func.plainto_tsquery(pg_config, query)
    )


def _concat_columns(columns: list[ColumnElement]) -> ColumnElement:
    return func.concat_ws(" ", *(func.coalesce(cast(column, String), "") for column in columns))


def _postgres_config_literal(config: str) -> ColumnElement:
    if config not in {"russian", "english", "simple"}:
        raise ValueError(f"Unsupported PostgreSQL text search config: {config}")
    return literal_column(f"'{config}'")
