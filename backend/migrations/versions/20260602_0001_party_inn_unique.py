"""Deduplicate parties by ИНН and add a partial unique index.

Fixes a concurrency bug: ``_upsert_party`` did check-then-insert with no unique
constraint, so parallel Celery workers processing invoices from the same
counterparty created duplicate Party rows. Subsequent INN lookups then raised
MultipleResultsFound and cascaded into lost invoices.

This migration merges any existing duplicates (repointing all FKs and merging
supplier profiles) and creates ``uq_parties_inn`` so duplicates can't recur.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260602_0001"
down_revision = "20260531_0003"
branch_labels = None
depends_on = None

# Every column that references parties.id (besides supplier_profiles.party_id,
# which is unique and handled separately by merging stats).
_FK_COLUMNS = [
    ("invoices", "supplier_id"),
    ("invoices", "buyer_id"),
    ("email_threads", "party_id"),
    ("price_history_entries", "supplier_id"),
    ("warehouse_receipts", "supplier_id"),
    ("supplier_contracts", "supplier_id"),
    ("tool_suppliers", "main_supplier_id"),
]


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # SQLite/others: best-effort unique index only (tests use a clean DB).
        op.create_index("uq_parties_inn", "parties", ["inn"], unique=True)
        return

    dupes = conn.execute(sa.text(
        """
        SELECT inn, array_agg(id ORDER BY created_at ASC) AS ids
        FROM parties
        WHERE inn IS NOT NULL
        GROUP BY inn
        HAVING count(*) > 1
        """
    )).fetchall()

    for _inn, ids in dupes:
        keep, drops = ids[0], ids[1:]
        for drop in drops:
            for table, col in _FK_COLUMNS:
                conn.execute(
                    sa.text(f"UPDATE {table} SET {col} = :keep WHERE {col} = :drop"),
                    {"keep": keep, "drop": drop},
                )
            # supplier_profiles.party_id is unique → merge stats, not repoint.
            keep_has_profile = conn.execute(
                sa.text("SELECT 1 FROM supplier_profiles WHERE party_id = :keep"),
                {"keep": keep},
            ).first()
            if keep_has_profile:
                conn.execute(sa.text(
                    """
                    UPDATE supplier_profiles k SET
                        total_invoices = COALESCE(k.total_invoices, 0) + COALESCE(d.total_invoices, 0),
                        total_amount   = COALESCE(k.total_amount, 0)   + COALESCE(d.total_amount, 0)
                    FROM supplier_profiles d
                    WHERE k.party_id = :keep AND d.party_id = :drop
                    """
                ), {"keep": keep, "drop": drop})
                conn.execute(
                    sa.text("DELETE FROM supplier_profiles WHERE party_id = :drop"),
                    {"drop": drop},
                )
            else:
                conn.execute(
                    sa.text("UPDATE supplier_profiles SET party_id = :keep WHERE party_id = :drop"),
                    {"keep": keep, "drop": drop},
                )
            conn.execute(sa.text("DELETE FROM parties WHERE id = :drop"), {"drop": drop})

    op.create_index(
        "uq_parties_inn",
        "parties",
        ["inn"],
        unique=True,
        postgresql_where=sa.text("inn IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_parties_inn", table_name="parties")
