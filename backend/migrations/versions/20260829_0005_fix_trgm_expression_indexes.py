"""Ф8: триграммные индексы были построены на выражении, которого нет в запросе.

``20260828_0003`` создала GIN-индексы по ``coalesce(subject,'')`` и
``coalesce(body_text,'')``. Планировщик использует expression-индекс только при
дословном совпадении выражения, а ``/api/email/search`` фильтрует по голой
колонке — поэтому индексы занимали 23 МБ и не использовались НИ РАЗУ:
на 100 тыс. писем поиск по телу шёл seq scan'ом, 1590 мс.

Замер после замены — в EMAIL_SUBSYSTEM_PLAN.md, раздел Ф8.

``20260829_0002`` не помогла: ``CREATE INDEX IF NOT EXISTS`` с тем же именем
молча ничего не делает, даже если определение другое. Поэтому здесь — явный
DROP, затем CREATE.

Revision ID: 20260829_0005
Revises: 20260829_0004
"""
from typing import Sequence, Union

from alembic import op

revision: str = "20260829_0005"
down_revision: Union[str, None] = "20260829_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index, table, column) — the column exactly as the query filters on it.
_REBUILD = (
    ("ix_email_messages_subject_trgm", "email_messages", "subject"),
    ("ix_email_messages_body_trgm", "email_messages", "body_text"),
    ("ix_email_contacts_name_trgm", "email_contacts", "name"),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in _REBUILD:
        op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(
            f"CREATE INDEX {name} ON {table} USING gin ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name, table, column in _REBUILD:
        op.execute(f"DROP INDEX IF EXISTS {name}")
        op.execute(
            f"CREATE INDEX {name} ON {table} "
            f"USING gin (coalesce({column},'') gin_trgm_ops)"
        )
