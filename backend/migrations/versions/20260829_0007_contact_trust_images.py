"""Ф1.4: «доверять картинкам этого отправителя».

Блокировка удалённых изображений по умолчанию защищает от трекинг-пикселя, но
без исключений её выключают целиком — и защита исчезает вся сразу. Доверие
хранится на контакте (per-user, потому что `email_contacts.owner_sub`), а
глобальное «всегда показывать» — в `users.preferences`, где уже живут прочие
пользовательские настройки.

Revision ID: 20260829_0007
Revises: 20260829_0006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0007"
down_revision: Union[str, None] = "20260829_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("email_contacts")}
    if "trust_images" not in cols:
        op.add_column(
            "email_contacts",
            sa.Column("trust_images", sa.Boolean(), nullable=False,
                      server_default="false"),
        )


def downgrade() -> None:
    op.drop_column("email_contacts", "trust_images")
