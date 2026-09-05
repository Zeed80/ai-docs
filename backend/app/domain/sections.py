"""Section access catalog — the workspace navigation tree used for per-user
section permissions.

Single source of truth (backend side) for which sections/subsections exist,
their stable keys, labels and routes. An admin grants a subset of these keys to
a user; anything not granted is hidden from that user's navigation and blocked
at the route level (UI redirect).

Access rules (see :func:`visible_section_keys`):
  * Admins always see every section.
  * Everyone sees the base sections (``BASE_SECTION_KEYS``) — currently just the
    "Сегодня" landing feed.
  * A regular user otherwise sees only the sections explicitly granted to them.
    An empty/absent grant therefore means "base sections only".

The keys MUST stay in sync with the frontend nav catalog
(``frontend/lib/nav-catalog.ts``) and the sidebar. They are stable identifiers
— never rename an existing key.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel


class SectionItem(BaseModel):
    key: str
    label: str
    href: str
    admin_only: bool = False


class SectionGroup(BaseModel):
    key: str
    label: str
    items: list[SectionItem]


# Always available to every authenticated user; not assignable (not shown in the
# admin checkbox tree). "feed" is the landing page; every authenticated user
# can also manage only their own durable work orders.
BASE_SECTION_KEYS: frozenset[str] = frozenset({"feed", "work_orders"})


SECTION_CATALOG: list[SectionGroup] = [
    SectionGroup(
        key="docs",
        label="Документы",
        items=[
            SectionItem(key="inbox", label="Входящие", href="/inbox"),
            SectionItem(key="documents", label="Документы", href="/documents"),
            SectionItem(key="invoices", label="Счета", href="/invoices"),
            SectionItem(key="handovers", label="Передачи", href="/handovers"),
            SectionItem(key="email", label="Почта", href="/email"),
        ],
    ),
    SectionGroup(
        key="engineering",
        label="Производство",
        items=[
            SectionItem(key="cad", label="CAD-редактор", href="/cad"),
            SectionItem(key="engineering", label="Инженерия", href="/engineering"),
            SectionItem(key="drawings", label="Чертежи", href="/drawings"),
            SectionItem(key="studio", label="Студия", href="/studio"),
            SectionItem(key="technology", label="Технология", href="/technology"),
            SectionItem(key="catalogs", label="Каталоги", href="/catalogs"),
        ],
    ),
    SectionGroup(
        key="warehouse",
        label="Склад",
        items=[
            SectionItem(key="warehouse", label="Склад", href="/warehouse"),
        ],
    ),
    SectionGroup(
        key="procurement",
        label="Закупки",
        items=[
            SectionItem(key="procurement", label="Закупка", href="/procurement"),
            SectionItem(key="suppliers", label="Поставщики", href="/suppliers"),
            SectionItem(key="compare", label="Сравнение КП", href="/compare"),
            SectionItem(key="cases", label="Кейсы", href="/cases"),
        ],
    ),
    SectionGroup(
        key="finance",
        label="Финансы",
        items=[
            SectionItem(key="payments", label="Платежи", href="/payments"),
            SectionItem(key="calendar", label="Календарь", href="/calendar"),
            SectionItem(key="approvals", label="Согласования", href="/approvals"),
        ],
    ),
    SectionGroup(
        key="data",
        label="Данные",
        items=[
            SectionItem(key="boms", label="Спецификации", href="/boms"),
            SectionItem(key="anomalies", label="Аномалии", href="/anomalies"),
            SectionItem(key="canonical", label="Канонизация", href="/canonical"),
            SectionItem(key="search", label="Поиск", href="/search"),
            SectionItem(key="ntd", label="НТД", href="/settings/ntd"),
            SectionItem(key="normalization", label="Нормализация", href="/settings/norm-cards"),
        ],
    ),
    SectionGroup(
        key="system",
        label="Система",
        items=[
            SectionItem(key="work_orders", label="Поручения", href="/work-orders"),
            SectionItem(key="quarantine", label="Карантин", href="/quarantine"),
            SectionItem(key="settings", label="Настройки", href="/settings"),
            SectionItem(
                key="admin",
                label="Администрирование",
                href="/admin",
                admin_only=True,
            ),
        ],
    ),
    SectionGroup(
        key="comms",
        label="Общение",
        items=[
            SectionItem(key="chat", label="Чат", href="/chat"),
        ],
    ),
]


ALL_SECTION_KEYS: frozenset[str] = frozenset(
    item.key for group in SECTION_CATALOG for item in group.items
)

# Keys an admin may assign via the GUI: everything except admin-only entries and
# the always-on base sections.
ASSIGNABLE_SECTION_KEYS: frozenset[str] = (
    frozenset(item.key for group in SECTION_CATALOG for item in group.items if not item.admin_only)
    - BASE_SECTION_KEYS
)


def _is_admin(roles: Iterable) -> bool:
    return any(str(getattr(r, "value", r)) == "admin" for r in roles)


def visible_section_keys(roles: Iterable, section_access: Iterable[str] | None) -> list[str]:
    """Resolve the set of section keys a user may see and use.

    Returns a sorted list. Admins get every section; everyone gets the base
    sections; regular users additionally get whatever was explicitly granted
    (unknown keys are ignored so a renamed/removed section can't leak access).
    """
    if _is_admin(roles):
        return sorted(ALL_SECTION_KEYS | BASE_SECTION_KEYS)
    granted = {k for k in (section_access or []) if k in ALL_SECTION_KEYS}
    return sorted(granted | set(BASE_SECTION_KEYS))


def validate_section_keys(keys: Iterable[str] | None) -> list[str]:
    """Keep only known, assignable keys (dedup, preserve first-seen order).

    Used when an admin saves a user's grant so we never persist stray/unknown
    keys or non-assignable ones (admin-only, base).
    """
    seen: list[str] = []
    for key in keys or []:
        if key in ASSIGNABLE_SECTION_KEYS and key not in seen:
            seen.append(key)
    return seen
