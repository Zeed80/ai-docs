"""Роль полного SQL-доступа агента: создаётся пустой, права выдаются флажком.

``agent_sql_reader_full`` нужна, когда оператор включил полный доступ моделей
к данным (``sql_full_access`` в конфиге агента). SELECT-права на все таблицы,
кроме секретных, считает и выдаёт ``app.ai.data_access.sync_full_reader_grants``
при включении и на старте — по живой схеме, чтобы таблица из будущей миграции
тоже попадала. Здесь роль только создаётся, без единого права: пока флажок
выключен, она ничего не читает.

Revision ID: 20261007_0001
Revises: 20261001_0001
"""

from alembic import op

revision = "20261007_0001"
down_revision = "20261001_0001"
branch_labels = None
depends_on = None

ROLE = "agent_sql_reader_full"


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT {ROLE} TO CURRENT_USER;")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE};")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE};")
    op.execute(f"DROP ROLE IF EXISTS {ROLE};")
