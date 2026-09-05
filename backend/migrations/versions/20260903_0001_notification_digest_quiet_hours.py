"""Дайджест и тихие часы для уведомлений.

Типы уведомлений были, а способа сказать «не пиши мне ночью» и «присылай
сводкой утром» — нет: на производстве поток пингов выключают целиком, вместе с
тем, ради чего его заводили.

Одна строка на пользователя: тихие часы (окно, в которое push не уходит) и
ежедневная сводка (час отправки). Значения по умолчанию сохраняют текущее
поведение — сводка выключена, тихих часов нет.

Revision ID: 20260903_0001
Revises: 20260829_0007
"""

import sqlalchemy as sa
from alembic import op

revision = "20260903_0001"
down_revision = "20260829_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "user_notification_settings" in inspector.get_table_names():
        return
    op.create_table(
        "user_notification_settings",
        sa.Column("user_sub", sa.String(255), primary_key=True),
        # Окно тишины в местном времени пользователя: 22 → 8 значит «с 22:00
        # до 08:00 не беспокоить». NULL = не настроено.
        sa.Column("quiet_from_hour", sa.Integer(), nullable=True),
        sa.Column("quiet_to_hour", sa.Integer(), nullable=True),
        sa.Column("digest_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("digest_hour", sa.Integer(), nullable=False, server_default="9"),
        sa.Column("last_digest_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("user_notification_settings")
