"""Отдельная роль для агентского SQL: рубеж, не зависящий от регулярных выражений.

Запрос к таблицам строит LLM по тексту, который может прийти из письма или
загруженного документа. Валидация в ``table_sql_pipeline`` разбирает FROM/JOIN
и сверяет со списком разрешённых таблиц, но это разбор текста: любая ошибка в
нём — и запрос уходит в базу как есть. ``transaction_read_only`` защищает от
записи и ничего не говорит о чтении, поэтому ``SELECT * FROM mailbox_configs``
такую проверку прошёл бы.

Роль ``agent_sql_reader`` закрывает это на уровне СУБД: у неё есть SELECT ровно
на те семь таблиц, которые агенту и разрешены. Права выданы явно, всё
остальное недоступно по умолчанию.

Роль без LOGIN: отдельный пул соединений не нужен, конвейер переключается на
неё через ``SET LOCAL ROLE`` в своей транзакции, а rollback возвращает исходную.
Чтобы переключение было возможно, владелец приложения объявлен членом роли.

Revision ID: 20260905_0001
Revises: 20260903_0003
"""

from alembic import op

revision = "20260905_0001"
down_revision = "20260903_0003"
branch_labels = None
depends_on = None

ROLE = "agent_sql_reader"

# Тот же список, что и ALLOWED_TABLES в table_sql_pipeline. Расхождение между
# ними означало бы, что один из двух рубежей шире другого, — за этим следит
# backend/tests/test_agent_sql_reader_role.py.
TABLES = (
    "documents",
    "invoices",
    "invoice_lines",
    "parties",
    "anomaly_cards",
    "approvals",
    "users",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
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
    # Членство даёт приложению право сделать SET ROLE на неё — и только это:
    # собственные права приложения при переключении не наследуются.
    op.execute(f"GRANT {ROLE} TO CURRENT_USER;")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE};")
    # На случай повторного применения к базе, где список уже менялся: сначала
    # снимаем всё, потом выдаём ровно нужное.
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE};")
    for table in TABLES:
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = '{table}'
                ) THEN
                    EXECUTE 'GRANT SELECT ON public.{table} TO {ROLE}';
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {ROLE};")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {ROLE};")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                DROP ROLE {ROLE};
            END IF;
        END
        $$;
        """
    )
