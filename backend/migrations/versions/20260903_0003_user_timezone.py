"""Часовой пояс — свойство пользователя, а не настроек уведомлений.

Зона появилась там, где сначала понадобилась: в тихих часах. Но это атрибут
человека, а не одной подсистемы — время писем, сроки счетов и любые даты в
интерфейсе относятся к нему же. Пока зона лежала в user_notification_settings,
каждый следующий потребитель либо читал бы чужую таблицу, либо заводил свою
копию.

Значения переносятся; колонка в настройках уведомлений удаляется, чтобы не
осталось второго источника правды.

Revision ID: 20260903_0003
Revises: 20260903_0002
"""

import sqlalchemy as sa
from alembic import op

revision = "20260903_0003"
down_revision = "20260903_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_columns = {c["name"] for c in inspector.get_columns("users")}
    if "timezone" not in user_columns:
        op.add_column("users", sa.Column("timezone", sa.String(64), nullable=True))

    if "user_notification_settings" in inspector.get_table_names():
        settings_columns = {c["name"] for c in inspector.get_columns("user_notification_settings")}
        if "timezone" in settings_columns:
            op.execute(
                sa.text(
                    "UPDATE users SET timezone = s.timezone "
                    "FROM user_notification_settings s "
                    "WHERE s.user_sub = users.sub "
                    "  AND s.timezone IS NOT NULL "
                    "  AND users.timezone IS NULL"
                )
            )
            op.drop_column("user_notification_settings", "timezone")


def downgrade() -> None:
    op.add_column("user_notification_settings", sa.Column("timezone", sa.String(64), nullable=True))
    op.execute(
        sa.text(
            "UPDATE user_notification_settings s SET timezone = u.timezone "
            "FROM users u WHERE u.sub = s.user_sub AND u.timezone IS NOT NULL"
        )
    )
    op.drop_column("users", "timezone")
