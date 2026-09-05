"""Периметр SQL, который пишет модель.

`workspace.sql_table` принимает задачу текстом, просит модель написать SQL и
выполняет его. Текст задачи может прийти из письма или документа, то есть
содержимое запроса не полностью под нашим контролем.

Защита была только чёрным списком ключевых слов: она не пускала запись, но
никак не ограничивала чтение — запрос к mailbox_configs, email_messages или
memory_facts проходил свободно, хотя модели показывают схему всего семи
таблиц.
"""

import pytest

from app.ai.table_sql_pipeline import (
    ALLOWED_TABLES,
    referenced_tables,
    validate_sql,
)


# ── Белый список таблиц ──────────────────────────────────────────────────────


def test_a_shown_table_is_allowed():
    assert validate_sql("SELECT id, total FROM invoices") is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM mailbox_configs",
        "SELECT * FROM email_messages",
        "SELECT * FROM provider_instances",
        "SELECT i.id FROM invoices i JOIN mailbox_configs m ON true",
    ],
)
def test_a_table_the_model_never_saw_is_refused(sql):
    assert validate_sql(sql) is None


def test_the_allowlist_matches_what_the_model_is_shown():
    """Если список схемы и список разрешённого разойдутся, модель начнёт
    получать отказы на таблицы, которые ей сами же и показали."""
    from app.ai import table_sql_pipeline as m

    import inspect

    src = inspect.getsource(m._get_schema_context)
    for table in ALLOWED_TABLES:
        assert f'"{table}"' in src, table


# ── Разбор ссылок на таблицы ─────────────────────────────────────────────────


def test_subqueries_and_ctes_are_not_mistaken_for_tables():
    sql = "SELECT * FROM (SELECT id FROM invoices) t"
    assert referenced_tables(sql) == {"invoices"}


def test_joins_are_collected_too():
    sql = "SELECT * FROM invoices JOIN parties ON parties.id = invoices.supplier_id"
    assert referenced_tables(sql) == {"invoices", "parties"}


# ── Комментарии и несколько операторов ───────────────────────────────────────


def test_a_comment_cannot_hide_a_forbidden_table():
    assert validate_sql("SELECT * FROM invoices -- , mailbox_configs") is not None
    assert validate_sql("SELECT * /* nice */ FROM mailbox_configs") is None


def test_a_comment_cannot_smuggle_a_dangerous_keyword():
    # Ключевое слово внутри комментария не должно валить безобидный запрос…
    assert validate_sql("SELECT id FROM invoices -- no DROP here") is not None
    # …а вне комментария — обязано.
    assert validate_sql("SELECT id FROM invoices; DROP TABLE invoices") is None


def test_a_second_statement_is_refused():
    assert validate_sql("SELECT id FROM invoices; SELECT 1 FROM parties") is None


def test_a_trailing_semicolon_is_fine():
    assert validate_sql("SELECT id FROM invoices;") is not None


# ── Прежние проверки не ослабли ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE invoices SET total = 0",
        "DELETE FROM invoices",
        "SELECT pg_read_file('/etc/passwd') FROM invoices",
        "SELECT current_setting('app.secret') FROM invoices",
    ],
)
def test_writes_and_system_functions_stay_refused(sql):
    assert validate_sql(sql) is None


def test_a_query_without_any_table_is_refused():
    """`SELECT version()` ничего полезного не даёт, а вот лишнее — может."""
    assert validate_sql("SELECT 1") is None
