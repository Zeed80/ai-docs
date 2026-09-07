"""Возможности модели, установленные живой пробой, а не взятые из каталога.

Каталог заполняется автоматически из ответа провайдера на ``/v1/models`` и
ошибается в обе стороны. На стенде встретились обе ошибки сразу:
``ollama_cloud deepseek-v3.1:671b`` объявлен БЕЗ зрения и при этом прочитал
чертёж; ``openrouter minimax-m3:free`` объявлен пригодным и строгую схему не
держит — три прохода полного чтения из пяти на нём отвалились.

Пока возможности не проверены, гейт назначения не может быть строгим: запретить
модель по недостоверным метаданным значит запретить работающую. Поэтому
несоответствие модальности блокирует назначение только когда возможность
ПРОВЕРЕНА, и остаётся предупреждением, пока о ней просто не знают. Чтобы это
различие существовало, результат пробы должен где-то храниться — и переживать
перезапуск: Redis на старте перезаполняется из Postgres, поэтому запись только
в Redis откатилась бы при первом же рестарте.

Отдельные колонки, а не ``model_catalog_runtime_entries``: тот оверлей
применяется через ``setdefault`` и запись, определённую в
``model_registry.yaml``, исправить не может в принципе. Ровно та же причина, по
которой рядом уже живёт ``thinking_levels``.

Revision ID: 20260907_0001
Revises: 20260905_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "20260907_0001"
down_revision = "20260905_0001"
branch_labels = None
depends_on = None

_TABLE = "model_runtime_overrides"


def _columns() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    existing = _columns()
    if not existing:
        # Пустая база собирается из metadata одним шагом — добавлять нечего.
        return
    if "capabilities" not in existing:
        op.add_column(_TABLE, sa.Column("capabilities", sa.JSON(), nullable=True))
    if "capabilities_checked_at" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("capabilities_checked_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    existing = _columns()
    if "capabilities_checked_at" in existing:
        op.drop_column(_TABLE, "capabilities_checked_at")
    if "capabilities" in existing:
        op.drop_column(_TABLE, "capabilities")
