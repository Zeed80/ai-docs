from __future__ import annotations

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.sql import column

from backend.app.db.text_search import ilike_condition, postgres_fts_condition


def test_ilike_condition_compiles_for_sqlite_fallback() -> None:
    compiled = str(
        ilike_condition([column("file_name"), column("text")], "ГОСТ").compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "lower" in compiled
    assert "LIKE lower('%ГОСТ%')" in compiled


def test_postgres_fts_condition_uses_russian_plain_query() -> None:
    compiled = str(
        postgres_fts_condition([column("file_name"), column("text")], "ГОСТ").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "to_tsvector('russian'" in compiled
    assert "@@ plainto_tsquery('russian', 'ГОСТ')" in compiled
    assert "concat_ws" in compiled
