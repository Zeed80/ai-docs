"""SQL-first table construction pipeline.

Instead of asking the LLM to "invent" table data (which leads to hallucination),
this pipeline:
  1. Reads SQLAlchemy model metadata → schema context
  2. LLM generates a SQL SELECT query using the real schema
  3. SQL is validated (syntax check + injection guard)
  4. Query is executed → real data from DB
  5. Python formats data into canvas block (no LLM involvement)
  6. LLM only writes a short title/description (trivial task)

This gives: zero hallucination, correct aggregations, fast formatting.
"""
from __future__ import annotations

import re
import time
from typing import Any

import structlog

logger = structlog.get_logger()

# ── Schema discovery ───────────────────────────────────────────────────────────

# Compact schema context injected into the LLM prompt.
# Updated lazily from SQLAlchemy metadata.
_SCHEMA_CACHE: dict[str, str] | None = None
_SCHEMA_MTIME: float = 0.0
_SCHEMA_TTL = 300.0  # rebuild every 5 minutes


def _get_schema_context() -> str:
    """Return a compact text representation of DB tables for LLM context."""
    global _SCHEMA_CACHE, _SCHEMA_MTIME
    now = time.time()
    if _SCHEMA_CACHE and (now - _SCHEMA_MTIME) < _SCHEMA_TTL:
        return _SCHEMA_CACHE.get("context", "")

    try:
        from app.db.models import (
            Document, Invoice, InvoiceLine, Party,
            AnomalyCard, Approval, User,
        )
        from app.db.session import engine  # noqa: F401

        # Build compact schema string
        lines: list[str] = []
        models = [
            ("documents", Document),
            ("invoices", Invoice),
            ("invoice_lines", InvoiceLine),
            ("parties", Party),
            ("anomaly_cards", AnomalyCard),
            ("approvals", Approval),
            ("users", User),
        ]
        for table_name, model in models:
            try:
                cols = []
                for col in model.__table__.columns:
                    col_type = str(col.type).split("(")[0]
                    nullable = "" if col.nullable else " NOT NULL"
                    cols.append(f"  {col.name} {col_type}{nullable}")
                lines.append(f"TABLE {table_name}:")
                lines.extend(cols)
                lines.append("")
            except Exception:
                pass

        context = "\n".join(lines)
        _SCHEMA_CACHE = {"context": context}
        _SCHEMA_MTIME = now
        return context
    except Exception as exc:
        logger.warning("table_pipeline_schema_failed", error=str(exc))
        return ""


# ── SQL validation ─────────────────────────────────────────────────────────────

# Dangerous patterns that must not appear in generated SQL
_INJECTION_PATTERNS = re.compile(
    r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE|GRANT|REVOKE"
    r"|COPY|pg_read_file|pg_write_file|pg_sleep|pg_ls_dir|pg_stat_file"
    r"|lo_import|lo_export|dblink|pg_reload_conf|current_setting|set_config)\b",
    re.IGNORECASE,
)

# Only allow SELECT statements
_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

# Maximum complexity guard
_MAX_SQL_CHARS = 4_000

# Таблицы, которые модель вообще видит: ровно те, чью схему ей показывает
# _get_schema_context(). Всё остальное — отказ.
#
# До этого проверка была только чёрным списком ключевых слов, то есть
# защищала от записи, но не от чтения: запрос к mailbox_configs,
# email_messages или memory_facts проходил свободно. Задачу пишет LLM по
# тексту, который может прийти из письма или документа, — то есть содержимое
# запроса не полностью под нашим контролем, и список разрешённого надёжнее
# списка запрещённого.
ALLOWED_TABLES = frozenset({
    "documents",
    "invoices",
    "invoice_lines",
    "parties",
    "anomaly_cards",
    "approvals",
    "users",
})

# FROM / JOIN <таблица>. Подзапросы и CTE тоже сюда попадают: после FROM у них
# стоит скобка, а не имя, и такое совпадение просто не матчится.
_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+(?!\()([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE
)

# Комментарии убираем ДО проверок: `SELECT 1 -- DROP TABLE x` и
# `/* */`-вставки иначе позволяют спрятать что угодно от регулярных выражений.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(sql: str) -> str:
    return _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", sql))


def referenced_tables(sql: str) -> set[str]:
    """Имена таблиц, к которым обращается запрос."""
    return {m.lower() for m in _TABLE_REF_RE.findall(_strip_comments(sql))}


def validate_sql(sql: str) -> str | None:
    """Return cleaned SQL if safe, or None if dangerous/invalid.

    Checks:
    - Must start with SELECT
    - Single statement
    - Only tables the model was actually shown
    - No DML/DDL keywords
    - Reasonable length
    """
    sql = sql.strip().rstrip(";")
    if not sql:
        return None

    probe = _strip_comments(sql)

    if not _SELECT_RE.match(probe.lstrip()):
        logger.warning("sql_pipeline_not_select", sql=sql[:80])
        return None
    # Точка с запятой внутри запроса означает второй оператор: одиночная
    # висящая на конце уже снята выше.
    if ";" in probe:
        logger.warning("sql_pipeline_multiple_statements", sql=sql[:80])
        return None
    if _INJECTION_PATTERNS.search(probe):
        logger.warning("sql_pipeline_injection_attempt", sql=sql[:80])
        return None

    tables = referenced_tables(sql)
    forbidden = tables - ALLOWED_TABLES
    if forbidden:
        logger.warning(
            "sql_pipeline_table_not_allowed",
            tables=sorted(forbidden),
            sql=sql[:120],
        )
        return None
    if not tables:
        # Запрос без единой таблицы ничего осмысленного не вернёт, а вот
        # `SELECT current_setting(...)` — вернёт лишнее.
        logger.warning("sql_pipeline_no_table", sql=sql[:80])
        return None

    if len(sql) > _MAX_SQL_CHARS:
        logger.warning("sql_pipeline_too_long", length=len(sql))
        return None
    return sql


# ── LLM SQL generation ─────────────────────────────────────────────────────────

_SQL_SYSTEM = """Ты SQL-эксперт для PostgreSQL. Генерируй ТОЛЬКО SELECT-запросы.
Возвращай ТОЛЬКО SQL без объяснений, без markdown, без кавычек.
Используй только таблицы из предоставленной схемы."""

_SQL_PROMPT = """\
Схема базы данных:
{schema}

Задача: {task}

Напиши один SQL SELECT-запрос, который даст данные для этой задачи.
Ограничения:
- LIMIT {limit} строк максимум
- Только READ-операции (SELECT)
- Используй только колонки из схемы выше
- Псевдонимы колонок на русском через AS для читаемости

SQL:"""

_TITLE_PROMPT = """\
Напиши краткое название таблицы (3-7 слов, на русском) для следующего запроса:
{task}

Только название, без кавычек и точки:"""


async def generate_sql(
    task: str,
    *,
    limit: int = 100,
    generate_fn=None,
) -> str | None:
    """Use LLM to generate a SQL query for the given task.

    Returns validated SQL or None on failure.
    """
    schema = _get_schema_context()
    if not schema:
        return None

    prompt = _SQL_PROMPT.format(schema=schema, task=task, limit=limit)

    try:
        if generate_fn is None:
            from app.ai.ollama_client import reasoning_generate
            raw = await reasoning_generate(prompt, system=_SQL_SYSTEM, format_json=False)
        else:
            raw = await generate_fn(prompt, _SQL_SYSTEM)

        # Extract SQL from response (strip prose/markdown)
        sql = _extract_sql(raw or "")
        return validate_sql(sql) if sql else None
    except Exception as exc:
        logger.warning("table_pipeline_sql_gen_failed", task=task[:80], error=str(exc))
        return None


def _extract_sql(text: str) -> str:
    """Extract SQL from LLM output (may be wrapped in markdown or prose)."""
    text = text.strip()

    # Markdown SQL fence
    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]*?)\s*```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Find bare SELECT
    m = re.search(r"(SELECT\b[\s\S]+)", text, re.IGNORECASE)
    if m:
        # Take until first double newline or end
        sql = re.split(r"\n\n", m.group(1))[0]
        return sql.strip()

    return text


# ── Query execution ────────────────────────────────────────────────────────────

async def execute_sql(sql: str, *, max_rows: int = 200) -> list[dict[str, Any]]:
    """Execute a validated SELECT query and return rows as list of dicts."""
    from sqlalchemy import text
    from app.db.session import _get_session_factory

    safe_sql = validate_sql(sql)
    if not safe_sql:
        raise ValueError("SQL failed validation")

    # Ограничение строк. Проверка была по любому вхождению LIMIT, поэтому
    # LIMIT внутри подзапроса подавлял внешний, и наружу мог уйти весь
    # результат. Смотрим только хвост запроса — там, где внешний LIMIT и
    # стоит.
    if not re.search(r"\bLIMIT\s+\d+\s*$", safe_sql, re.IGNORECASE):
        safe_sql = f"{safe_sql} LIMIT {max_rows}"

    async with _get_session_factory()() as db:
        # Второй рубеж, не зависящий от качества регулярных выражений: сама
        # транзакция объявлена только на чтение, а долгий запрос обрывается по
        # таймауту, а не занимает соединение до бесконечности.
        await db.execute(text("SET LOCAL transaction_read_only = on"))
        await db.execute(text("SET LOCAL statement_timeout = '15s'"))
        result = await db.execute(text(safe_sql))
        cols = list(result.keys())
        rows = [dict(zip(cols, row)) for row in result.fetchall()]
        await db.rollback()
    return rows


# ── Canvas block formatting ────────────────────────────────────────────────────

def format_as_canvas_table(
    rows: list[dict[str, Any]],
    *,
    title: str = "Таблица",
    description: str = "",
    task: str = "",
) -> dict[str, Any]:
    """Convert DB rows to a workspace canvas block dict.

    No LLM involvement — pure Python formatting.
    """
    if not rows:
        return {
            "type": "table",
            "title": title,
            "description": description or "Нет данных",
            "columns": [],
            "rows": [],
            "total_rows": 0,
            "generated_by": "sql_pipeline",
        }

    columns = [
        {"key": col, "label": col.replace("_", " ").title()}
        for col in rows[0].keys()
    ]

    formatted_rows = []
    for row in rows:
        formatted_row = {}
        for col, val in row.items():
            if val is None:
                formatted_row[col] = "—"
            elif hasattr(val, "isoformat"):
                formatted_row[col] = val.isoformat()
            elif isinstance(val, float):
                formatted_row[col] = round(val, 2)
            else:
                formatted_row[col] = val
        formatted_rows.append(formatted_row)

    return {
        "type": "table",
        "title": title,
        "description": description,
        "columns": columns,
        "rows": formatted_rows,
        "total_rows": len(rows),
        "generated_by": "sql_pipeline",
        "source_task": task[:200] if task else "",
    }


# ── High-level pipeline entry point ───────────────────────────────────────────

async def build_table_from_task(
    task: str,
    *,
    limit: int = 100,
    generate_fn=None,
) -> dict[str, Any]:
    """Full pipeline: task description → canvas table block.

    Steps:
      1. Generate SQL from task (LLM with schema context)
      2. Validate SQL
      3. Execute against DB
      4. Format to canvas block
      5. Generate title (tiny LLM call)

    Returns canvas block dict or error dict.
    """
    logger.info("table_pipeline_start", task=task[:80])

    # Step 1-2: Generate + validate SQL
    sql = await generate_sql(task, limit=limit, generate_fn=generate_fn)
    if not sql:
        return {"status": "error", "message": "Не удалось сгенерировать SQL-запрос для задачи"}

    # Step 3: Execute
    try:
        rows = await execute_sql(sql, max_rows=limit)
    except Exception as exc:
        logger.warning("table_pipeline_execute_failed", sql=sql[:200], error=str(exc))
        return {"status": "error", "message": f"Ошибка выполнения запроса: {exc}"}

    # Step 4: Format
    block = format_as_canvas_table(rows, task=task)

    # Step 5: Generate title (cheap LLM call)
    try:
        if generate_fn is not None:
            raw_title = await generate_fn(_TITLE_PROMPT.format(task=task), None)
        else:
            from app.ai.ollama_client import reasoning_generate
            raw_title = await reasoning_generate(
                _TITLE_PROMPT.format(task=task),
                system="Дай краткое название таблице. Только название — без кавычек.",
                format_json=False,
            )
        if raw_title:
            title = raw_title.strip().strip('"\'«»').split("\n")[0][:80]
            block["title"] = title or block["title"]
    except Exception:
        pass  # title is optional

    logger.info(
        "table_pipeline_done",
        task=task[:60],
        rows=len(rows),
        title=block.get("title"),
    )
    return {"status": "ok", "data": block, "sql": sql}
