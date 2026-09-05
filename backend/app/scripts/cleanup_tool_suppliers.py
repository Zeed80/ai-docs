"""Э7: clean up ToolSupplier rows and link them to real counterparties.

Found on the live database while fixing catalog upload: 22 of 23 ToolSuppliers
had no main_supplier_id, so even successfully created entries never showed on
the supplier card (it looks the catalog up by party_id), and two rows were named
with a literal "${steps.discover_suppliers.output.suppliers[N].name}" — an
unresolved plan placeholder that reached entity creation (root cause fixed in
domain/work_planning.py).

Dry-run by default:
    python -m app.scripts.cleanup_tool_suppliers            # report only
    python -m app.scripts.cleanup_tool_suppliers --apply    # make the changes
"""

from __future__ import annotations

import argparse
import asyncio
import re

from sqlalchemy import select

from app.db.models import Party, ToolCatalogEntry, ToolSupplier
from app.db.session import _get_session_factory

PLACEHOLDER = re.compile(r"\$\{|\}")


def normalize_name(name: str) -> str:
    lowered = name.lower().replace("ё", "е")
    lowered = re.sub(r"\b(ооо|оао|зао|ао|ип|пао|тд|торговый дом|llc|ltd|gmbh)\b", " ", lowered)
    return re.sub(r"[^a-zа-я0-9]+", "", lowered)


async def run(apply: bool) -> dict:
    factory = _get_session_factory()
    report = {"deleted": [], "linked": [], "unmatched": [], "merged": []}

    async with factory() as db:
        suppliers = (await db.execute(select(ToolSupplier))).scalars().all()
        parties = (await db.execute(select(Party))).scalars().all()
        by_name = {normalize_name(p.name): p for p in parties if p.name}
        by_inn = {p.inn: p for p in parties if getattr(p, "inn", None)}

        for supplier in suppliers:
            if PLACEHOLDER.search(supplier.name or ""):
                entries = (
                    (
                        await db.execute(
                            select(ToolCatalogEntry).where(
                                ToolCatalogEntry.supplier_id == supplier.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                report["deleted"].append(
                    {"id": str(supplier.id), "name": supplier.name, "entries": len(entries)}
                )
                if apply:
                    for entry in entries:
                        await db.delete(entry)
                    await db.delete(supplier)
                continue

            if supplier.main_supplier_id:
                continue
            party = None
            contact = supplier.contact_info or {}
            inn = contact.get("inn") if isinstance(contact, dict) else None
            if inn and inn in by_inn:
                party = by_inn[inn]
            elif normalize_name(supplier.name or "") in by_name:
                party = by_name[normalize_name(supplier.name or "")]

            if party is None:
                report["unmatched"].append({"id": str(supplier.id), "name": supplier.name})
                continue
            report["linked"].append(
                {"id": str(supplier.id), "name": supplier.name, "party": party.name}
            )
            if apply:
                supplier.main_supplier_id = party.id

        # Duplicates: the live database had YG1 ×3, Haltec ×4 and Betar ×4 —
        # each web-discovery run created another row for the same vendor. Keep
        # the oldest, move its entries over, drop the rest.
        survivors: dict[str, ToolSupplier] = {}
        for supplier in sorted(suppliers, key=lambda s: s.created_at):
            if PLACEHOLDER.search(supplier.name or ""):
                continue
            key = normalize_name(supplier.name or "")
            if not key:
                continue
            keeper = survivors.get(key)
            if keeper is None:
                survivors[key] = supplier
                continue
            moved = (
                (
                    await db.execute(
                        select(ToolCatalogEntry).where(ToolCatalogEntry.supplier_id == supplier.id)
                    )
                )
                .scalars()
                .all()
            )
            report["merged"].append(
                {
                    "name": supplier.name,
                    "into": str(keeper.id),
                    "entries_moved": len(moved),
                }
            )
            if apply:
                for entry in moved:
                    entry.supplier_id = keeper.id
                if keeper.main_supplier_id is None and supplier.main_supplier_id:
                    keeper.main_supplier_id = supplier.main_supplier_id
                if not keeper.website and supplier.website:
                    keeper.website = supplier.website
                await db.flush()
                await db.delete(supplier)

        if apply:
            await db.commit()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()

    report = asyncio.run(run(args.apply))
    mode = "ПРИМЕНЕНО" if args.apply else "ПРОБНЫЙ ПРОГОН (изменений нет)"
    print(f"— {mode} —")
    print(f"Удалить мусорных поставщиков: {len(report['deleted'])}")
    for item in report["deleted"]:
        print(f"  · {item['name'][:80]} (позиций: {item['entries']})")
    print(f"Связать с контрагентом: {len(report['linked'])}")
    for item in report["linked"]:
        print(f"  · {item['name'][:50]} → {item['party'][:50]}")
    print(f"Объединить дубли: {len(report['merged'])}")
    for item in report["merged"]:
        print(f"  · {item['name'][:60]} → {item['into'][:8]} (позиций: {item['entries_moved']})")
    print(f"Не сопоставлено (нужна ручная привязка): {len(report['unmatched'])}")
    for item in report["unmatched"]:
        print(f"  · {item['name'][:80]}")


if __name__ == "__main__":
    main()
