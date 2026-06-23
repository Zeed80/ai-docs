"""Guarded SQL pipeline: only validated read-only SELECT is allowed through."""

from __future__ import annotations

from app.ai.table_sql_pipeline import validate_sql


def test_plain_select_passes():
    assert validate_sql("SELECT id, total_amount FROM invoices LIMIT 10") is not None


def test_trailing_semicolon_stripped():
    out = validate_sql("SELECT 1;")
    assert out == "SELECT 1"


def test_non_select_rejected():
    for sql in ("UPDATE invoices SET total_amount = 0",
                "DELETE FROM invoices",
                "INSERT INTO invoices (id) VALUES ('x')",
                "DROP TABLE invoices",
                "ALTER TABLE invoices ADD COLUMN x int"):
        assert validate_sql(sql) is None, sql


def test_injection_and_dangerous_functions_rejected():
    for sql in ("SELECT * FROM invoices; DROP TABLE invoices",
                "SELECT pg_read_file('/etc/passwd')",
                "SELECT * FROM invoices WHERE 1=1 UNION SELECT * FROM users; DELETE FROM x"):
        assert validate_sql(sql) is None, sql
