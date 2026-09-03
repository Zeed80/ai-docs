"""Часовой пояс пользователя для тихих часов и сводки.

Тихие часы считались по времени сервера: для одной площадки это верно, а для
распределённой команды «не беспокоить с 22 до 8» означало чужие 22:00. Час
сводки страдал так же — приходила не утром, а когда утро наступило на сервере.

Хранится IANA-имя зоны («Europe/Moscow»). NULL = как раньше, по серверу.

Revision ID: 20260903_0002
Revises: 20260903_0001
"""

from alembic import op
import sqlalchemy as sa

revision = "20260903_0002"
down_revision = "20260903_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_notification_settings" not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns("user_notification_settings")}
    if "timezone" not in columns:
        op.add_column(
            "user_notification_settings",
            sa.Column("timezone", sa.String(64), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("user_notification_settings", "timezone")
