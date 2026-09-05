"""Периметр агентского SQL держится на трёх рубежах, а не на одном.

Задачу для SQL пишет LLM по тексту, который может прийти из письма или
загруженного документа. Первый рубеж — разбор FROM/JOIN и белый список; это
разбор текста, и ошибка в нём пропускает запрос как есть. Второй —
`transaction_read_only`, он защищает от записи и ничего не говорит о чтении.
Третий — роль `agent_sql_reader` с SELECT ровно на разрешённые таблицы: это
права СУБД, они не зависят от качества регулярных выражений.

Здесь проверяется, что рубежи описывают ОДИН И ТОТ ЖЕ набор таблиц. Разойдясь,
они перестают быть тремя рубежами: более широкий просто перестаёт что-либо
ограничивать.
"""

from __future__ import annotations

import pathlib
import re

from app.ai.table_sql_pipeline import _READER_ROLE, ALLOWED_TABLES

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260905_0001_agent_sql_reader_role.py"
)


def _migration_tables() -> set[str]:
    source = _MIGRATION.read_text(encoding="utf-8")
    block = re.search(r"^TABLES = \(\n(.*?)^\)", source, re.S | re.M)
    assert block, "в миграции не найден кортеж TABLES"
    return set(re.findall(r'"([a-z_]+)"', block.group(1)))


def test_migration_grants_exactly_the_allowed_tables():
    assert _migration_tables() == set(ALLOWED_TABLES), (
        "список таблиц в миграции разошёлся с ALLOWED_TABLES — один из рубежей "
        "стал шире другого и перестал ограничивать"
    )


def test_role_name_matches_the_migration():
    assert f'ROLE = "{_READER_ROLE}"' in _MIGRATION.read_text(encoding="utf-8")


def test_role_cannot_log_in():
    """Роль переключается через SET ROLE в уже открытой транзакции.

    С LOGIN она стала бы ещё одной учётной записью с паролем — лишняя
    поверхность там, где отдельное соединение не нужно.
    """
    assert "CREATE ROLE {ROLE} NOLOGIN" in _MIGRATION.read_text(encoding="utf-8")


def test_allowed_tables_carry_no_secrets():
    """Явный список того, чего агенту видеть нельзя.

    Тест сторожит обратное направление: расширение ALLOWED_TABLES почтой,
    настройками ящиков или памятью — это уже не «таблица для отчёта».
    """
    forbidden = {
        "mailbox_configs",
        "email_messages",
        "email_drafts",
        "memory_facts",
        "provider_instances",
        "agent_config",
    }
    assert not (set(ALLOWED_TABLES) & forbidden)
