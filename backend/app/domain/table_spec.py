"""Declarative table engine: «таблица = спецификация, данные = SQL».

The LLM (or a deterministic parser) produces a small :class:`TableSpec` —
which columns from which source, filters, sort. The engine compiles it to one
SQLAlchemy query and returns the FULL result set (true ``total``, hard cap
``MAX_ROWS`` with an explicit ``truncated`` flag). Rows never pass through a
language model, so the table is always complete and instant.

The spec is stored inside the workspace block, so edits («добавь столбец с НДС
перед суммой») are patch operations on the stored spec followed by a re-run —
no LLM needed for recognised commands (see :func:`parse_patch_command`).

Safety: specs can only reference whitelisted fields of curated sources — the
model never writes SQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import (
    Numeric, Select, Text, and_, case, cast, distinct, func, literal, or_, select,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AnomalyCard,
    CanonicalItem,
    Document,
    Drawing,
    EmailMessage,
    InventoryItem,
    Invoice,
    InvoiceLine,
    Party,
    PaymentSchedule,
    Project,
    SiteObject,
)

logger = structlog.get_logger()

MAX_ROWS = 5000  # hard cap on rendered rows; `total` is always the true count


# ── Spec models ────────────────────────────────────────────────────────────────


class ColumnSpec(BaseModel):
    field: str
    header: str | None = None
    # Aggregate function applied to this column under group_by (avg/min/max for
    # «сравни цены / средняя / самая дешёвая», count for «сколько»). None → the
    # default: SUM for numbers, string_agg(distinct) for text.
    agg: Literal["sum", "avg", "min", "max", "count"] | None = None

    @field_validator("agg", mode="before")
    @classmethod
    def _lenient_agg(cls, value: Any) -> str | None:
        v = str(value).strip().lower() if value is not None else None
        aliases = {"average": "avg", "mean": "avg", "minimum": "min",
                   "maximum": "max", "summa": "sum", "сумма": "sum",
                   "средн": "avg", "минимум": "min", "максимум": "max"}
        if v in {"sum", "avg", "min", "max", "count"}:
            return v
        return aliases.get(v)

    @field_validator("header", mode="before")
    @classmethod
    def _lenient_header(cls, value: Any) -> str | None:
        # LLM-built specs sometimes ship junk types (true/1/{}); a bad header
        # must not 422 the whole table — fall back to the catalog header.
        return value if isinstance(value, str) and value.strip() else None


class FilterSpec(BaseModel):
    field: str
    op: Literal["eq", "ne", "contains", "gte", "lte", "between", "in", "smart"] = "contains"
    value: Any = None
    value2: Any | None = None  # upper bound for "between"

    @field_validator("op", mode="before")
    @classmethod
    def _lenient_op(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        v = value.strip().lower()
        # Models reach for SQL "like"/"ilike" — map them to the engine's substring
        # op so the call doesn't silently return 0 and force a wasted retry.
        if v in ("like", "ilike"):
            return "contains"
        return v


class SortSpec(BaseModel):
    field: str
    dir: Literal["asc", "desc"] = "asc"

    @field_validator("dir", mode="before")
    @classmethod
    def _lenient_dir(cls, value: Any) -> Any:
        # Accept common LLM variants: DESC/descending/убыв…
        if isinstance(value, str):
            v = value.strip().lower()
            if v.startswith(("desc", "убыв")):
                return "desc"
            if v.startswith(("asc", "возр")):
                return "asc"
        return value


class TableSpec(BaseModel):
    source: str
    title: str = ""
    columns: list[ColumnSpec] = Field(default_factory=list)
    filters: list[FilterSpec] = Field(default_factory=list)
    sort: list[SortSpec] = Field(default_factory=list)
    # Cluster rows so all rows sharing these field values sit together ("объедини/
    # сгруппируй по поставщику"). All detail rows are kept; group_by fields become
    # the PRIMARY sort keys, the explicit `sort` applies within each group.
    group_by: list[str] = Field(default_factory=list)
    limit: int | None = None  # None → all rows (up to MAX_ROWS)


# ── Field catalog (semantic layer) ─────────────────────────────────────────────


@dataclass(frozen=True)
class FieldDef:
    key: str
    header: str
    type: str = "text"  # text | number | date
    synonyms: tuple[str, ...] = ()
    # Smart filters / NL commands target text fields; mark the main one.
    primary_text: bool = False


@dataclass(frozen=True)
class SourceDef:
    key: str
    title: str
    synonyms: tuple[str, ...]
    fields: dict[str, FieldDef]
    default_columns: tuple[str, ...]

    @property
    def primary_text_field(self) -> str | None:
        for fd in self.fields.values():
            if fd.primary_text:
                return fd.key
        return None


def _fields(*defs: FieldDef) -> dict[str, FieldDef]:
    return {d.key: d for d in defs}


SOURCES: dict[str, SourceDef] = {
    "invoices": SourceDef(
        key="invoices",
        title="Счета",
        synonyms=("счет", "счета", "счетов", "инвойс", "invoice"),
        fields=_fields(
            FieldDef("invoice_number", "Номер счета", "text",
                     ("номер", "номер счета", "№")),
            FieldDef("invoice_date", "Дата счета", "date",
                     ("дата", "дата счета")),
            FieldDef("due_date", "Срок оплаты", "date",
                     ("срок", "срок оплаты", "оплатить до")),
            FieldDef("supplier_name", "Поставщик", "text",
                     ("поставщик", "контрагент", "продавец"), primary_text=True),
            FieldDef("supplier_inn", "ИНН поставщика", "text",
                     ("инн",)),
            FieldDef("subtotal", "Сумма без НДС", "number",
                     ("без ндс", "сумма без ндс", "подытог")),
            FieldDef("tax_amount", "НДС", "number",
                     ("ндс", "налог", "сумма ндс")),
            FieldDef("total_amount", "Сумма", "number",
                     ("сумма", "итого", "общая сумма", "сумма счета", "стоимость")),
            FieldDef("currency", "Валюта", "text", ("валюта",)),
            FieldDef("status", "Статус", "text", ("статус",)),
            FieldDef("items_list", "Перечень товаров", "text",
                     ("товары", "перечень товаров", "позиции", "состав",
                      "перечень", "номенклатура")),
            FieldDef("items_count", "Кол-во позиций", "number",
                     ("количество позиций", "кол-во позиций", "число позиций")),
        ),
        default_columns=("supplier_name", "invoice_number", "invoice_date", "total_amount"),
    ),
    "invoice_items": SourceDef(
        key="invoice_items",
        title="Позиции счетов",
        synonyms=("товар", "товары", "позиции", "строки", "номенклатура", "items"),
        fields=_fields(
            FieldDef("description", "Наименование", "text",
                     ("товар", "наименование", "позиция", "описание"), primary_text=True),
            FieldDef("sku", "Артикул", "text", ("артикул", "код")),
            FieldDef("quantity", "Кол-во", "number", ("количество", "кол-во")),
            FieldDef("unit", "Ед.", "text", ("единица", "ед", "ед.изм")),
            FieldDef("unit_price", "Цена", "number", ("цена", "цена за единицу")),
            FieldDef("amount", "Сумма", "number", ("сумма", "стоимость", "итого")),
            FieldDef("tax_rate", "Ставка НДС", "number", ("ставка ндс", "ставка")),
            FieldDef("tax_amount", "НДС", "number", ("ндс", "налог")),
            FieldDef("invoice_number", "Номер счета", "text", ("номер счета", "счет")),
            FieldDef("invoice_date", "Дата счета", "date", ("дата", "дата счета")),
            FieldDef("supplier_name", "Поставщик", "text", ("поставщик", "контрагент")),
        ),
        default_columns=("description", "quantity", "unit", "unit_price", "amount",
                         "supplier_name", "invoice_number"),
    ),
    "suppliers": SourceDef(
        key="suppliers",
        title="Поставщики",
        synonyms=("поставщик", "поставщики", "контрагент", "контрагенты"),
        fields=_fields(
            FieldDef("name", "Название", "text",
                     ("название", "имя", "поставщик"), primary_text=True),
            FieldDef("inn", "ИНН", "text", ("инн",)),
            FieldDef("kpp", "КПП", "text", ("кпп",)),
            FieldDef("address", "Адрес", "text", ("адрес",)),
            FieldDef("bank_name", "Банк", "text", ("банк",)),
            FieldDef("bank_bik", "БИК", "text", ("бик",)),
        ),
        default_columns=("name", "inn", "address"),
    ),
    "warehouse": SourceDef(
        key="warehouse",
        title="Остатки склада",
        synonyms=("склад", "остатки", "остаток", "запасы", "в наличии", "тмц"),
        fields=_fields(
            FieldDef("name", "Наименование", "text",
                     ("наименование", "товар", "позиция", "материал"), primary_text=True),
            FieldDef("sku", "Артикул", "text", ("артикул", "код")),
            FieldDef("current_qty", "Остаток", "number",
                     ("остаток", "количество", "кол-во", "в наличии", "запас")),
            FieldDef("unit", "Ед.", "text", ("единица", "ед", "ед.изм")),
            FieldDef("min_qty", "Мин. остаток", "number",
                     ("минимальный остаток", "минимум", "неснижаемый запас")),
            FieldDef("location", "Место хранения", "text",
                     ("место", "место хранения", "расположение", "ячейка", "локация")),
            FieldDef("below_min", "Ниже минимума", "text",
                     ("ниже минимума", "дефицит", "нехватка", "требует заказа")),
        ),
        default_columns=("name", "sku", "current_qty", "unit", "location"),
    ),
    "documents": SourceDef(
        key="documents",
        title="Документы",
        synonyms=("документ", "документы", "файл", "файлы", "акт", "акты",
                  "накладн", "договор", "договоры", "кп", "коммерческое предложение"),
        fields=_fields(
            FieldDef("file_name", "Файл", "text",
                     ("файл", "имя файла", "название"), primary_text=True),
            FieldDef("doc_type", "Тип", "text",
                     ("тип", "тип документа", "вид")),
            FieldDef("status", "Статус", "text", ("статус", "состояние")),
            FieldDef("created_at", "Загружен", "date",
                     ("дата", "дата загрузки", "загружен", "получен")),
            FieldDef("source_channel", "Источник", "text",
                     ("источник", "канал", "откуда")),
            FieldDef("page_count", "Страниц", "number",
                     ("страниц", "страницы", "кол-во страниц")),
            FieldDef("doc_type_confidence", "Уверенность", "number",
                     ("уверенность", "достоверность", "confidence")),
            FieldDef("project", "Проект", "text",
                     ("проект", "проекту", "проекта", "стройка", "объект строительства")),
            FieldDef("object", "Объект", "text",
                     ("объект", "объекту", "объекта", "площадка", "узел")),
        ),
        default_columns=("file_name", "doc_type", "status", "created_at"),
    ),
    "payments": SourceDef(
        key="payments",
        title="Платежи",
        synonyms=("платеж", "платежи", "оплата", "оплаты", "график платежей",
                  "просрочка", "просрочки", "к оплате"),
        fields=_fields(
            FieldDef("due_date", "Срок оплаты", "date",
                     ("срок", "срок оплаты", "дата оплаты", "оплатить до")),
            FieldDef("amount", "Сумма", "number", ("сумма", "сумма платежа")),
            FieldDef("currency", "Валюта", "text", ("валюта",)),
            FieldDef("status", "Статус", "text",
                     ("статус", "состояние")),  # scheduled|paid|overdue|partial|cancelled
            FieldDef("paid_at", "Оплачен", "date", ("оплачен", "дата платежа")),
            FieldDef("paid_amount", "Оплачено", "number", ("оплачено",)),
            FieldDef("invoice_number", "Номер счета", "text",
                     ("номер счета", "счет"), primary_text=True),
            FieldDef("supplier_name", "Поставщик", "text", ("поставщик", "контрагент")),
            FieldDef("reference", "Назначение", "text", ("назначение", "референс")),
        ),
        default_columns=("supplier_name", "invoice_number", "due_date", "amount", "status"),
    ),
    "anomalies": SourceDef(
        key="anomalies",
        title="Аномалии",
        synonyms=("аномалия", "аномалии", "расхождение", "расхождения", "проблемы"),
        fields=_fields(
            FieldDef("title", "Аномалия", "text",
                     ("название", "заголовок", "аномалия"), primary_text=True),
            FieldDef("anomaly_type", "Тип", "text", ("тип", "вид")),
            FieldDef("severity", "Критичность", "text",
                     ("критичность", "серьезность", "важность")),
            FieldDef("status", "Статус", "text", ("статус", "состояние")),
            FieldDef("created_at", "Обнаружена", "date",
                     ("дата", "обнаружена", "создана")),
            FieldDef("resolved_by", "Решил", "text", ("решил", "кто решил")),
            FieldDef("resolved_at", "Решена", "date", ("решена", "дата решения")),
            FieldDef("description", "Описание", "text", ("описание", "детали")),
        ),
        default_columns=("title", "anomaly_type", "severity", "status", "created_at"),
    ),
    "emails": SourceDef(
        key="emails",
        title="Письма",
        synonyms=("письмо", "письма", "почта", "email", "входящие", "корреспонденция"),
        fields=_fields(
            FieldDef("subject", "Тема", "text",
                     ("тема", "заголовок"), primary_text=True),
            FieldDef("from_address", "От кого", "text",
                     ("от кого", "отправитель", "адрес")),
            FieldDef("mailbox", "Ящик", "text", ("ящик", "почтовый ящик")),
            FieldDef("received_at", "Получено", "date",
                     ("получено", "дата", "дата получения")),
            FieldDef("attachment_count", "Вложений", "number",
                     ("вложений", "кол-во вложений")),
            FieldDef("has_attachments", "Вложения", "text",
                     ("вложения", "с вложениями")),
            FieldDef("direction", "Направление", "text",
                     ("направление", "входящее", "исходящее")),
        ),
        default_columns=("subject", "from_address", "received_at", "attachment_count"),
    ),
    "drawings": SourceDef(
        key="drawings",
        title="Чертежи",
        synonyms=("чертеж", "чертежи", "кд", "конструкторская документация"),
        fields=_fields(
            FieldDef("drawing_number", "Обозначение", "text",
                     ("обозначение", "номер чертежа", "децимальный номер")),
            FieldDef("filename", "Файл", "text",
                     ("файл", "имя файла"), primary_text=True),
            FieldDef("title", "Наименование", "text",
                     ("наименование", "название", "деталь")),
            FieldDef("material", "Материал", "text", ("материал",)),
            FieldDef("revision", "Ревизия", "text", ("ревизия", "изменение")),
            FieldDef("format", "Формат", "text", ("формат",)),
            FieldDef("drawing_type", "Тип", "text",
                     ("тип", "вид")),  # detail|assembly|section|weld
            FieldDef("part_class", "Класс детали", "text",
                     ("класс", "класс детали")),
            FieldDef("status", "Статус", "text", ("статус", "состояние")),
            FieldDef("created_at", "Загружен", "date", ("дата", "загружен")),
        ),
        default_columns=("drawing_number", "title", "material", "drawing_type", "status"),
    ),
    # ── Virtual sources (not SQL tables) ────────────────────────────────────
    # Resolved by provider functions, not the SQL engine, but exposed in the
    # same catalog so the agent picks them like any other table.
    "vector_search": SourceDef(
        key="vector_search",
        title="Семантический поиск",
        synonyms=("похож", "похожие", "семантический", "по смыслу", "найти похожие",
                  "релевантные документы"),
        fields=_fields(
            FieldDef("query", "Запрос", "text",
                     ("запрос", "текст", "поиск"), primary_text=True),
            FieldDef("score", "Релевантность", "number", ("релевантность", "score")),
            FieldDef("file_name", "Документ", "text", ("документ", "файл", "имя")),
            FieldDef("doc_type", "Тип", "text", ("тип", "вид")),
            FieldDef("status", "Статус", "text", ("статус",)),
            FieldDef("snippet", "Фрагмент", "text", ("фрагмент", "сниппет", "текст")),
            FieldDef("doc_id", "ID документа", "text", ("id", "идентификатор")),
        ),
        default_columns=("score", "file_name", "doc_type", "status"),
    ),
    "graph_query": SourceDef(
        key="graph_query",
        title="Связи (граф памяти)",
        synonyms=("связи", "граф", "связан", "отношения", "окружение",
                  "с кем связан", "что связано"),
        fields=_fields(
            FieldDef("start_node", "Сущность", "text",
                     ("сущность", "узел", "от", "вокруг"), primary_text=True),
            FieldDef("mode", "Режим", "text", ("режим",)),  # neighborhood|path
            FieldDef("target", "Цель", "text", ("цель", "до")),
            FieldDef("source_title", "От", "text", ("от", "источник")),
            FieldDef("edge_type", "Связь", "text", ("связь", "тип связи", "отношение")),
            FieldDef("target_title", "К", "text", ("к", "цель")),
            FieldDef("target_type", "Тип узла", "text", ("тип узла", "тип")),
            FieldDef("confidence", "Уверенность", "number", ("уверенность",)),
            FieldDef("reason", "Причина", "text", ("причина", "обоснование")),
        ),
        default_columns=("source_title", "edge_type", "target_title", "target_type"),
    ),
}

# Sources resolved by provider functions instead of the SQL engine.
VIRTUAL_SOURCES: frozenset[str] = frozenset({"vector_search", "graph_query"})


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е").strip()


# Russian case/number endings, longest first; stripped once per word.
_RU_ENDINGS = (
    "иями", "ями", "ами", "ыми", "ими", "ого", "его", "ому", "ему",
    "ой", "ей", "ом", "ем", "ам", "ям", "ах", "ях", "ую", "юю",
    "ая", "яя", "ое", "ее", "ие", "ые", "ия", "ии", "ию",
    "а", "я", "о", "е", "и", "ы", "у", "ю", "ь", "й",
)


def _stem(word: str) -> str:
    """Strip one case/number ending: «суммой»→«сумм», «дате»→«дат»."""
    for ending in _RU_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            return word[: -len(ending)]
    return word


_VOWELS = "аеёиоуыэюя"


def _drop_fleeting_vowel(stem: str) -> str:
    """«остаток»→«остатк», «перечен(ь)»→«перечн» — беглая гласная перед
    последней согласной выпадает в косвенных падежах."""
    if len(stem) >= 4 and stem[-2] in _VOWELS and stem[-1] not in _VOWELS:
        return stem[:-2] + stem[-1]
    return stem


def _words_match(a: str, b: str) -> bool:
    """Declension-tolerant word equality: «суммой»/«сумме»/«сумма» and
    «остатка»/«остаток» match; «сумма»/«сумка», «номер»/«номенклатура» do not."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 3:
        return False
    sa, sb = _stem(a), _stem(b)
    if sa == sb:
        return True
    # Fleeting vowel: compare with the pre-final vowel dropped on either side.
    return _drop_fleeting_vowel(sa) == sb or sa == _drop_fleeting_vowel(sb)


def _phrases_match(a: str, b: str) -> bool:
    wa, wb = a.split(), b.split()
    return len(wa) == len(wb) and all(_words_match(x, y) for x, y in zip(wa, wb, strict=True))


def resolve_field(source: SourceDef, token: str) -> FieldDef | None:
    """Resolve a user word («ндс», «суммой», «сумме») to a catalog field."""
    t = _norm(token).strip(" .,!?:;«»\"'")
    if not t:
        return None
    if t in source.fields:
        return source.fields[t]
    # Exact match first, then declension-folded match («перед суммой» → total_amount).
    for exact in (True, False):
        for fd in source.fields.values():
            candidates = [_norm(fd.header), *(_norm(s) for s in fd.synonyms)]
            for c in candidates:
                if not c:
                    continue
                if exact and t == c:
                    return fd
                if not exact and _phrases_match(t, c):
                    return fd
    return None


def resolve_source(text: str) -> SourceDef | None:
    t = _norm(text)
    for src in SOURCES.values():
        if any(s in t for s in src.synonyms):
            return src
    return None


# ── Query builders per source ──────────────────────────────────────────────────


def invoice_items_list_subquery():
    """Correlated scalar subquery: newline-joined item list for an Invoice.

    Format per line: ``описание — кол-во ед.`` ordered by ``line_number``.
    Canonical source of the items-list format — reused by the invoices table
    API (``/api/tables/query``) so the column matches agent spec-tables.
    """
    line_text = func.coalesce(InvoiceLine.description, "—") + func.coalesce(
        " — " + func.trim(func.to_char(InvoiceLine.quantity, "FM999999990.###"))
        + " " + func.coalesce(InvoiceLine.unit, "шт"),
        "",
    )
    return (
        select(
            func.string_agg(
                line_text,
                aggregate_order_by(literal("\n"), InvoiceLine.line_number),
            )
        )
        .where(InvoiceLine.invoice_id == Invoice.id)
        .correlate(Invoice)
        .scalar_subquery()
    )


# Backwards-compatible private alias used within this module.
_items_list_subq = invoice_items_list_subquery


def _items_count_subq():
    return (
        select(func.count(InvoiceLine.id))
        .where(InvoiceLine.invoice_id == Invoice.id)
        .correlate(Invoice)
        .scalar_subquery()
    )


def _invoices_exprs() -> dict[str, Any]:
    return {
        "invoice_number": Invoice.invoice_number,
        "invoice_date": Invoice.invoice_date,
        "due_date": Invoice.due_date,
        "supplier_name": Party.name,
        "supplier_inn": Party.inn,
        "subtotal": Invoice.subtotal,
        "tax_amount": Invoice.tax_amount,
        "total_amount": Invoice.total_amount,
        "currency": Invoice.currency,
        "status": Invoice.status,
        "items_list": _items_list_subq(),
        "items_count": _items_count_subq(),
    }


def _invoice_items_exprs() -> dict[str, Any]:
    return {
        "description": InvoiceLine.description,
        "sku": InvoiceLine.sku,
        "quantity": InvoiceLine.quantity,
        "unit": InvoiceLine.unit,
        "unit_price": InvoiceLine.unit_price,
        "amount": InvoiceLine.amount,
        "tax_rate": InvoiceLine.tax_rate,
        "tax_amount": InvoiceLine.tax_amount,
        "invoice_number": Invoice.invoice_number,
        "invoice_date": Invoice.invoice_date,
        "supplier_name": Party.name,
    }


def _suppliers_exprs() -> dict[str, Any]:
    return {
        "name": Party.name,
        "inn": Party.inn,
        "kpp": Party.kpp,
        "address": Party.address,
        "bank_name": Party.bank_name,
        "bank_bik": Party.bank_bik,
    }


def _warehouse_exprs() -> dict[str, Any]:
    return {
        "name": InventoryItem.name,
        "sku": InventoryItem.sku,
        "current_qty": InventoryItem.current_qty,
        "unit": InventoryItem.unit,
        "min_qty": InventoryItem.min_qty,
        "location": InventoryItem.location,
        "below_min": case(
            (
                and_(
                    InventoryItem.min_qty.isnot(None),
                    InventoryItem.current_qty < InventoryItem.min_qty,
                ),
                "Да",
            ),
            else_="Нет",
        ),
    }


def _documents_exprs() -> dict[str, Any]:
    return {
        "file_name": Document.file_name,
        "doc_type": Document.doc_type,
        "status": Document.status,
        "created_at": Document.created_at,
        "source_channel": Document.source_channel,
        "page_count": Document.page_count,
        "doc_type_confidence": Document.doc_type_confidence,
        "project": Project.name,
        "object": SiteObject.name,
    }


def _payments_exprs() -> dict[str, Any]:
    return {
        "due_date": PaymentSchedule.due_date,
        "amount": PaymentSchedule.amount,
        "currency": PaymentSchedule.currency,
        "status": PaymentSchedule.status,
        "paid_at": PaymentSchedule.paid_at,
        "paid_amount": PaymentSchedule.paid_amount,
        "invoice_number": Invoice.invoice_number,
        "supplier_name": Party.name,
        "reference": PaymentSchedule.reference,
    }


def _anomalies_exprs() -> dict[str, Any]:
    return {
        "title": AnomalyCard.title,
        "anomaly_type": AnomalyCard.anomaly_type,
        "severity": AnomalyCard.severity,
        "status": AnomalyCard.status,
        "created_at": AnomalyCard.created_at,
        "resolved_by": AnomalyCard.resolved_by,
        "resolved_at": AnomalyCard.resolved_at,
        "description": AnomalyCard.description,
    }


def _emails_exprs() -> dict[str, Any]:
    return {
        "subject": EmailMessage.subject,
        "from_address": EmailMessage.from_address,
        "mailbox": EmailMessage.mailbox,
        "received_at": EmailMessage.received_at,
        "attachment_count": EmailMessage.attachment_count,
        "has_attachments": case(
            (EmailMessage.has_attachments.is_(True), "Да"), else_="Нет"
        ),
        "direction": case(
            (EmailMessage.is_inbound.is_(True), "Входящее"), else_="Исходящее"
        ),
    }


def _drawings_exprs() -> dict[str, Any]:
    return {
        "drawing_number": Drawing.drawing_number,
        "filename": Drawing.filename,
        # title_block — JSON шаблонной надписи: {title, material, ...}
        "title": Drawing.title_block["title"].as_string(),
        "material": Drawing.title_block["material"].as_string(),
        "revision": Drawing.revision,
        "format": Drawing.format,
        "drawing_type": Drawing.drawing_type,
        "part_class": Drawing.part_class,
        "status": Drawing.status,
        "created_at": Drawing.created_at,
    }


def _base_stmt(
    source_key: str,
    exprs: dict[str, Any],
    keys: list[str],
    select_cols: list[Any] | None = None,
) -> Select:
    # ``select_cols`` overrides the default per-key columns (used by aggregating
    # group_by to select group keys + aggregate expressions over the same joins).
    cols = select_cols if select_cols is not None else [exprs[k].label(k) for k in keys]
    if source_key == "invoices":
        return select(*cols).select_from(Invoice).outerjoin(
            Party, Invoice.supplier_id == Party.id
        )
    if source_key == "invoice_items":
        return (
            select(*cols)
            .select_from(InvoiceLine)
            .join(Invoice, InvoiceLine.invoice_id == Invoice.id)
            .outerjoin(Party, Invoice.supplier_id == Party.id)
        )
    if source_key == "suppliers":
        return select(*cols).select_from(Party)
    if source_key == "warehouse":
        return select(*cols).select_from(InventoryItem)
    if source_key == "documents":
        return (
            select(*cols)
            .select_from(Document)
            .outerjoin(Project, Document.project_id == Project.id)
            .outerjoin(SiteObject, Document.object_id == SiteObject.id)
        )
    if source_key == "payments":
        return (
            select(*cols)
            .select_from(PaymentSchedule)
            .join(Invoice, PaymentSchedule.invoice_id == Invoice.id)
            .outerjoin(Party, Invoice.supplier_id == Party.id)
        )
    if source_key == "anomalies":
        return select(*cols).select_from(AnomalyCard)
    if source_key == "emails":
        return select(*cols).select_from(EmailMessage)
    if source_key == "drawings":
        return select(*cols).select_from(Drawing)
    raise ValueError(f"Unknown table source: {source_key}")


_EXPRS = {
    "invoices": _invoices_exprs,
    "invoice_items": _invoice_items_exprs,
    "suppliers": _suppliers_exprs,
    "warehouse": _warehouse_exprs,
    "documents": _documents_exprs,
    "payments": _payments_exprs,
    "anomalies": _anomalies_exprs,
    "emails": _emails_exprs,
    "drawings": _drawings_exprs,
}


# ── Writeback (editable spec-tables → DB through approval) ────────────────────
#
# Only own-model scalar fields are writable; joined fields (e.g. invoices'
# supplier_name from Party) stay read-only. Each spec field key here equals the
# model attribute (verified against the *_exprs maps above). Edits NEVER touch
# the DB directly from here — the spec-table cell-edit endpoint files a
# DraftAction routed through the table.apply_diff approval gate; only the
# approved-action executor calls :func:`apply_cell_writeback`.


@dataclass(frozen=True)
class WritebackSpec:
    entity_type: str
    model: Any
    editable: frozenset[str]
    numeric_fields: frozenset[str] = frozenset()
    date_fields: frozenset[str] = frozenset()


WRITEBACK: dict[str, WritebackSpec] = {
    "invoices": WritebackSpec(
        entity_type="invoice",
        model=Invoice,
        editable=frozenset({
            "invoice_number", "invoice_date", "due_date",
            "subtotal", "tax_amount", "total_amount", "currency",
        }),
        numeric_fields=frozenset({"subtotal", "tax_amount", "total_amount"}),
        date_fields=frozenset({"invoice_date", "due_date"}),
    ),
    "suppliers": WritebackSpec(
        entity_type="supplier",
        model=Party,
        editable=frozenset({"name", "inn", "kpp", "address", "bank_name", "bank_bik"}),
    ),
    "warehouse": WritebackSpec(
        entity_type="inventory_item",
        model=InventoryItem,
        editable=frozenset({"name", "sku", "current_qty", "unit", "min_qty", "location"}),
        numeric_fields=frozenset({"current_qty", "min_qty"}),
    ),
}


def writeback_for(source_key: str) -> WritebackSpec | None:
    return WRITEBACK.get(source_key)


def _pk_expr(source_key: str) -> Any | None:
    wb = WRITEBACK.get(source_key)
    return wb.model.id if wb is not None else None


def coerce_writeback_value(source_key: str, field: str, value: Any) -> Any:
    """Coerce a string cell value to the column's Python type."""
    wb = WRITEBACK[source_key]
    if value is None or value == "":
        return None
    if field in wb.numeric_fields:
        return float(str(value).replace(" ", "").replace(",", "."))
    if field in wb.date_fields:
        from datetime import date, datetime

        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        if isinstance(value, (date, datetime)):
            return value
        raise ValueError(f"Не разобрана дата: {value!r}")
    return str(value)


async def apply_cell_writeback(
    db: AsyncSession, source_key: str, pk: Any, field: str, value: Any
) -> tuple[bool, str]:
    """Apply one approved cell edit. Returns (ok, message). Caller commits."""
    wb = WRITEBACK.get(source_key)
    if wb is None or field not in wb.editable:
        return False, f"Поле «{field}» нередактируемо для источника «{source_key}»"
    obj = (
        await db.execute(select(wb.model).where(wb.model.id == pk))
    ).scalar_one_or_none()
    if obj is None:
        return False, "Строка не найдена"
    try:
        setattr(obj, field, coerce_writeback_value(source_key, field, value))
    except ValueError as exc:
        return False, str(exc)
    return True, "ok"

# Fields whose filters must use the aggregate-free line text (smart search over
# invoice contents): filtering invoices by «items_list» means EXISTS over lines.
_INVOICE_ITEMS_TEXT_FIELD = "items_list"


# ── Smart filter ───────────────────────────────────────────────────────────────

_NUM_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
_STOP_TOKENS = frozenset({
    "для", "под", "из", "на", "по", "с", "и", "или", "все", "всех", "только",
    "диаметр", "диаметра", "диаметром",  # qualifier words; the number itself matters
})


def _smart_tokens(query: str) -> list[str]:
    """Search tokens from a fuzzy query: stems for words, exact numbers."""
    tokens: list[str] = []
    for raw in _norm(query).replace(",", " ").split():
        word = raw.strip(" .!?:;«»\"'()")
        if not word or word in _STOP_TOKENS:
            continue
        if _NUM_RE.match(word):
            tokens.append(word)
        elif len(word) >= 3:
            # Poor-man stemming: chop endings so «фрезы»/«фреза»/«фрез» match,
            # but keep at least 4 chars to avoid noise like «фре» → «фрейм».
            stem = word[: max(4, len(word) - 2)] if len(word) > 4 else word
            tokens.append(stem)
    return tokens[:6]


async def _canonical_expansion(db: AsyncSession, tokens: list[str]) -> list[Any]:
    """Canonical-item ids whose name/aliases match all word tokens.

    Connects fuzzy queries to the normalization layer: «фрезы» finds canonical
    items (and through them differently-worded invoice lines).
    """
    word_tokens = [t for t in tokens if not _NUM_RE.match(t)]
    if not word_tokens:
        return []
    conds = [
        or_(
            CanonicalItem.name.ilike(f"%{tok}%"),
            cast(CanonicalItem.aliases, Text).ilike(f"%{tok}%"),
        )
        for tok in word_tokens
    ]
    try:
        rows = (await db.execute(select(CanonicalItem.id).where(and_(*conds)).limit(200))).all()
        return [row[0] for row in rows]
    except Exception as exc:
        logger.debug("canonical_expansion_failed", error=str(exc))
        return []


def _number_condition(column: Any, token: str) -> Any:
    """Match a number as a standalone token inside text (5 ≠ 50 ≠ 0.5)."""
    value = token.replace(",", ".")
    pattern = rf"(^|[^0-9.,]){re.escape(value)}([^0-9]|$)"
    return column.op("~")(pattern)


# Smart-filter targets per source: (text column, canonical-item id column).
def _smart_targets(source_key: str) -> tuple[Any, Any | None] | None:
    return {
        "invoices": (InvoiceLine.description, InvoiceLine.canonical_item_id),
        "invoice_items": (InvoiceLine.description, InvoiceLine.canonical_item_id),
        "warehouse": (InventoryItem.name, InventoryItem.canonical_item_id),
        "suppliers": (Party.name, None),
        "documents": (Document.file_name, None),
        "payments": (Party.name, None),
        "anomalies": (AnomalyCard.title, None),
        "emails": (EmailMessage.subject, None),
        "drawings": (Drawing.filename, None),
    }.get(source_key)


async def _smart_text_condition(
    db: AsyncSession, source_key: str, query: str
) -> Any | None:
    """AND-of-tokens condition over the source's item text, with canonical expansion."""
    tokens = _smart_tokens(query)
    if not tokens:
        return None
    targets = _smart_targets(source_key)
    if targets is None:
        return None
    text_col, canonical_col = targets

    canonical_ids = await _canonical_expansion(db, tokens) if canonical_col is not None else []

    per_token: list[Any] = []
    for tok in tokens:
        if _NUM_RE.match(tok):
            per_token.append(_number_condition(text_col, tok))
        else:
            cond = text_col.ilike(f"%{tok}%")
            if canonical_ids:
                cond = or_(cond, canonical_col.in_(canonical_ids))
            per_token.append(cond)
    line_cond = and_(*per_token)

    if source_key == "invoices":
        # Invoice qualifies when ANY of its lines matches.
        return (
            select(InvoiceLine.id)
            .where(and_(InvoiceLine.invoice_id == Invoice.id, line_cond))
            .correlate(Invoice)
            .exists()
        )
    return line_cond


# ── Executor ───────────────────────────────────────────────────────────────────


class TableResult(BaseModel):
    columns: list[dict]
    rows: list[dict]
    total: int
    truncated: bool = False


def _format_value(value: Any, ftype: str) -> Any:
    if value is None:
        return None
    if ftype == "date":
        return value.strftime("%d.%m.%Y") if hasattr(value, "strftime") else str(value)
    if ftype == "number" and isinstance(value, float):
        return round(value, 2)
    if hasattr(value, "value"):  # enums (InvoiceStatus)
        return value.value
    return value


def validate_spec(spec: TableSpec) -> list[str]:
    """Structural errors a model-produced spec can contain."""
    problems: list[str] = []
    source = SOURCES.get(spec.source)
    if source is None:
        return [f"unknown source {spec.source!r}; allowed: {sorted(SOURCES)}"]
    for col in spec.columns:
        if col.field not in source.fields:
            problems.append(f"unknown column field {col.field!r} for source {spec.source!r}")
    for flt in spec.filters:
        if flt.op != "smart" and flt.field not in source.fields:
            problems.append(f"unknown filter field {flt.field!r}")
    for srt in spec.sort:
        if srt.field not in source.fields:
            problems.append(f"unknown sort field {srt.field!r}")
    for gb in spec.group_by:
        if gb not in source.fields:
            problems.append(f"unknown group_by field {gb!r}")
    return problems


_TABLE_VERBS = (
    "покажи", "показать", "выведи", "вывести", "выбери", "отбери", "сравни",
    "сравнить", "сгруппир", "объедини", "посчитай", "сосчитай", "таблиц",
    "сколько", "средн", "в разрезе", "по каждому", "построй", "список",
)
_DOC_CONTENT_MARKERS = (
    "о чём", "о чем", "напомни", "найди похож", "перескажи", "краткое содержан",
    "текст письма", "что написано", "суть документа", "расскажи про", "对",
)


async def correct_category_error(
    db: AsyncSession, spec: TableSpec, user_text: str
) -> TableSpec | None:
    """Fix a category error: a spec on ``suppliers`` filtering NAME by a term that
    matches NO supplier but DOES match invoice line items («выведи фрезы по
    поставщику» built as suppliers.name ~ «фреза»). Data-driven (not a lexicon):
    rebuild on ``invoice_items`` with a smart filter on the item description, and
    group by supplier when the request says so. Returns None when not applicable."""
    if spec.source != "suppliers":
        return None
    vals = [
        str(f.value) for f in spec.filters
        if f.field == "name" and f.op in ("smart", "contains") and f.value
    ]
    for v in vals:
        term = v.strip().strip("%").strip()
        if len(term) < 3:
            continue
        sup_n = (await db.execute(
            select(func.count(Party.id)).where(Party.name.ilike(f"%{term}%"))
        )).scalar() or 0
        if sup_n:
            continue  # a legitimate supplier-name filter — leave it
        item_n = (await db.execute(
            select(func.count(InvoiceLine.id)).where(InvoiceLine.description.ilike(f"%{term}%"))
        )).scalar() or 0
        if not item_n:
            continue  # not an item term either — don't touch
        group_by = ["supplier_name"] if re.search(r"поставщик", (user_text or "").lower()) else []
        cols = [ColumnSpec(field=f) for f in
                ("supplier_name", "description", "quantity", "unit_price", "amount")]
        return TableSpec(
            source="invoice_items", title=spec.title or "Позиции счетов",
            columns=cols,
            filters=[FilterSpec(field="description", op="smart", value=term)],
            group_by=group_by,
        )
    return None


def is_spec_table_request(text: str) -> bool:
    """Catalog-grounded: a clear request to BUILD A TABLE over structured data —
    a table verb plus a reference to a catalog source/field — and NOT a
    document-content question. Lets the orchestrator force structured grounding
    so an obvious table request is never answered via slow RAG."""
    t = (text or "").lower()
    if any(m in t for m in _DOC_CONTENT_MARKERS):
        return False
    if not any(v in t for v in _TABLE_VERBS):
        return False
    for src in SOURCES.values():
        if any(s.lower() in t for s in (src.title, *src.synonyms)):
            return True
        for fd in src.fields.values():
            if any(syn.lower() in t for syn in (fd.header, *fd.synonyms)):
                return True
    return False


def _resolve_source(name: str) -> str | None:
    """Map a free/near-miss source name to a catalog key via synonyms."""
    n = (name or "").strip().lower()
    if n in SOURCES:
        return n
    for key, src in SOURCES.items():
        cands = {key.lower(), src.title.lower(), *(s.lower() for s in src.synonyms)}
        if n in cands:
            return key
    for key, src in SOURCES.items():
        for s in (key, src.title, *src.synonyms):
            s = (s or "").lower()
            if s and (n in s or s in n) and min(len(n), len(s)) >= 4:
                return key
    return None


def _heal_filter(source: SourceDef, flt: "FilterSpec") -> "FilterSpec | None":
    """Heal an off-catalog non-smart filter. Crucially, a numeric pseudo-field
    («amount_min», «сумма_от», «min_total») maps to the primary numeric field
    with gte/lte, and a search pseudo-field («search_text», «query») becomes a
    smart filter on the primary text — so the constraint is NOT silently lost."""
    fd = resolve_field(source, flt.field)
    if fd:
        return flt.model_copy(update={"field": fd.key})
    raw = (flt.field or "").lower()
    if any(k in raw for k in ("search", "query", "keyword", "текст", "поиск")):
        return flt.model_copy(update={
            "field": source.primary_text_field or "description", "op": "smart"})
    op = flt.op
    if re.search(r"(_min\b|_от\b|_from\b|^min[_ ]|^от[_ ])", raw):
        op = "gte"
    elif re.search(r"(_max\b|_до\b|_to\b|^max[_ ]|^до[_ ])", raw):
        op = "lte"
    base = re.sub(r"(_min|_max|_от|_до|_from|_to|^min_|^max_|^от_|^до_)", "", raw).strip()
    healed = resolve_field(source, base)
    if healed is None and any(
        w in raw for w in ("amount", "sum", "сумм", "total", "итог", "price",
                           "цен", "qty", "кол", "quant", "stoim", "стоим")):
        healed = _primary_number_field(source, base)
    if healed is not None:
        return flt.model_copy(update={"field": healed.key, "op": op})
    return None


def repair_spec_to_catalog(spec: TableSpec) -> tuple[TableSpec, list[str]]:
    """Heal a worker-produced spec against the whitelisted catalog: map a near-miss
    source/field name to its catalog key (via synonyms + morphology-aware
    ``resolve_field``), drop the truly unresolvable. Returns (spec, change-notes).
    Deterministic — keeps the agent grounded so a slightly-off spec is honoured,
    not 422'd or silently mis-executed."""
    notes: list[str] = []
    src_key = spec.source if spec.source in SOURCES else _resolve_source(spec.source)
    if src_key is None:
        return spec, notes  # validate_spec will reject with a helpful message
    spec = spec.model_copy(deep=True)
    if src_key != spec.source:
        notes.append(f"источник «{spec.source}» → «{src_key}»")
        spec.source = src_key
    source = SOURCES[src_key]

    def _heal(field: str, header: str | None = None) -> str | None:
        if field in source.fields:
            return field
        fd = resolve_field(source, field) or (
            resolve_field(source, header) if header else None)
        return fd.key if fd else None

    new_cols: list[ColumnSpec] = []
    for col in spec.columns:
        healed = _heal(col.field, col.header)
        if healed is None:
            notes.append(f"убрал неизвестную колонку «{col.field}»")
        else:
            if healed != col.field:
                notes.append(f"колонка «{col.field}» → «{source.fields[healed].header}»")
            new_cols.append(col.model_copy(update={"field": healed}))
    spec.columns = new_cols

    new_filters: list[FilterSpec] = []
    for flt in spec.filters:
        if flt.op == "smart":
            new_filters.append(flt)
            continue
        healed_flt = _heal_filter(source, flt)
        if healed_flt is None:
            # A dropped FILTER returns wrong (unfiltered) data — surface it loudly.
            notes.append(f"⚠ убрал неизвестный фильтр «{flt.field}»")
        else:
            if (healed_flt.field, healed_flt.op) != (flt.field, flt.op):
                tail = f" ({healed_flt.op})" if healed_flt.op != flt.op else ""
                notes.append(
                    f"фильтр «{flt.field}» → «{source.fields[healed_flt.field].header}»{tail}")
            new_filters.append(healed_flt)
    spec.filters = new_filters

    new_sort: list[SortSpec] = []
    for srt in spec.sort:
        healed = _heal(srt.field)
        if healed is not None:
            if healed != srt.field:
                notes.append(f"сортировка «{srt.field}» → «{source.fields[healed].header}»")
            new_sort.append(srt.model_copy(update={"field": healed}))
    spec.sort = new_sort

    new_gb: list[str] = []
    for gb in spec.group_by:
        healed = _heal(gb)
        if healed is not None:
            if healed != gb:
                notes.append(f"группировка «{gb}» → «{source.fields[healed].header}»")
            new_gb.append(healed)
    spec.group_by = new_gb

    return spec, notes


def _filter_value(spec: TableSpec, field: str) -> str | None:
    for flt in spec.filters:
        if flt.field == field and flt.value not in (None, ""):
            return str(flt.value)
    return None


def _finalize_virtual(spec: TableSpec, all_rows: list[dict]) -> TableResult:
    """Project/sort/limit provider rows into a TableResult like the SQL path."""
    source = SOURCES[spec.source]
    columns = list(spec.columns) or [ColumnSpec(field=f) for f in source.default_columns]
    keys = [c.field for c in columns]
    field_defs = [source.fields[k] for k in keys]

    total = len(all_rows)
    rows = all_rows
    for srt in reversed(spec.sort):
        if srt.field in source.fields:
            rows = sorted(
                rows,
                key=lambda r: (r.get(srt.field) is None, r.get(srt.field)),
                reverse=(srt.dir == "desc"),
            )
    cap = min(spec.limit or MAX_ROWS, MAX_ROWS)
    rows = rows[:cap]

    out_rows = [
        {fd.key: _format_value(r.get(fd.key), fd.type) for fd in field_defs}
        for r in rows
    ]
    out_columns = [
        {"key": fd.key, "header": col.header or fd.header, "type": fd.type,
         "editable": False}
        for col, fd in zip(columns, field_defs, strict=True)
    ]
    return TableResult(
        columns=out_columns, rows=out_rows, total=total,
        truncated=total > len(out_rows),
    )


async def _execute_vector_search(db: AsyncSession, spec: TableSpec) -> TableResult:
    """Semantic search over the document vector store, as a table."""
    query = _filter_value(spec, "query")
    if not query:
        raise ValueError(
            "vector_search требует фильтр {field: query, op: contains, value: '<текст>'}"
        )
    from app.ai.embeddings import embed_text
    from app.vector.qdrant_store import search_similar

    doc_type = _filter_value(spec, "doc_type")
    cap = min(spec.limit or 50, MAX_ROWS)
    try:
        vector = await embed_text(query, task_type="query")  # confidential=local
        hits = search_similar(vector, limit=cap, doc_type=doc_type)
    except Exception as exc:  # Qdrant/embedding unavailable — degrade, don't crash
        logger.warning("vector_search_failed", error=str(exc))
        raise ValueError(f"Семантический поиск недоступен: {exc}") from exc
    rows = [
        {
            "score": round(float(h.get("score") or 0.0), 3),
            "file_name": h.get("file_name") or h.get("payload", {}).get("file_name") or "",
            "doc_type": h.get("doc_type") or "",
            "status": h.get("status") or "",
            "snippet": (h.get("payload", {}).get("text")
                        or h.get("payload", {}).get("snippet") or "")[:300],
            "doc_id": str(h.get("doc_id") or ""),
            "query": query,
        }
        for h in hits
    ]
    return _finalize_virtual(spec, rows)


async def _execute_graph_query(db: AsyncSession, spec: TableSpec) -> TableResult:
    """Relationships around an entity in the knowledge graph, as a table."""
    start = _filter_value(spec, "start_node")
    if not start:
        raise ValueError(
            "graph_query требует фильтр {field: start_node, op: contains, value: '<сущность>'}"
        )
    from app.db.models import KnowledgeEdge, KnowledgeNode

    center = (
        await db.execute(
            select(KnowledgeNode)
            .where(
                or_(
                    KnowledgeNode.title.ilike(f"%{start}%"),
                    KnowledgeNode.canonical_key.ilike(f"%{start}%"),
                )
            )
            .order_by(KnowledgeNode.confidence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if center is None:
        return _finalize_virtual(spec, [])

    edges = (
        await db.execute(
            select(KnowledgeEdge).where(
                or_(
                    KnowledgeEdge.source_node_id == center.id,
                    KnowledgeEdge.target_node_id == center.id,
                )
            )
        )
    ).scalars().all()
    node_ids = {center.id}
    for e in edges:
        node_ids.add(e.source_node_id)
        node_ids.add(e.target_node_id)
    nodes = (
        await db.execute(select(KnowledgeNode).where(KnowledgeNode.id.in_(node_ids)))
    ).scalars().all()
    by_id = {n.id: n for n in nodes}

    rows = []
    for e in edges:
        src = by_id.get(e.source_node_id)
        tgt = by_id.get(e.target_node_id)
        rows.append({
            "start_node": center.title,
            "source_title": src.title if src else "",
            "edge_type": e.edge_type,
            "target_title": tgt.title if tgt else "",
            "target_type": tgt.node_type if tgt else "",
            "confidence": round(float(e.confidence or 0.0), 3),
            "reason": e.reason or "",
        })
    return _finalize_virtual(spec, rows)


_VIRTUAL_PROVIDERS = {
    "vector_search": _execute_vector_search,
    "graph_query": _execute_graph_query,
}


# Columns that are already SQL aggregates (string_agg/count subqueries) — they
# cannot be re-aggregated, so a grouped spec containing them falls back to the
# detail-row clustering mode instead of true GROUP BY.
_AGG_SKIP_COLUMNS: dict[str, set[str]] = {
    "invoices": {"items_list", "items_count"},
}


def _can_aggregate(source_key: str, keys: list[str]) -> bool:
    skip = _AGG_SKIP_COLUMNS.get(source_key, set())
    return not any(k in skip for k in keys)


async def _execute_grouped(
    db: AsyncSession,
    spec: TableSpec,
    source: SourceDef,
    columns: list[ColumnSpec],
    keys: list[str],
    exprs: dict[str, Any],
    where_conds: list[Any],
) -> TableResult:
    """One row per group: group key(s) + string_agg(distinct) / SUM aggregates."""
    field_defs = [source.fields[k] for k in keys]
    group_keys = [g for g in spec.group_by if g in exprs]
    select_cols: list[Any] = []
    for col, fd in zip(columns, field_defs, strict=True):
        e = exprs[col.field]
        if col.field in group_keys:
            select_cols.append(e.label(col.field))
            continue
        agg = col.agg or ("sum" if fd.type == "number" else "concat")
        if agg == "concat":
            select_cols.append(
                func.string_agg(distinct(cast(e, Text)), literal("; ")).label(col.field)
            )
        elif agg == "count":
            select_cols.append(func.count(e).label(col.field))
        elif agg == "avg":
            select_cols.append(func.round(cast(func.avg(e), Numeric), 2).label(col.field))
        else:  # sum / min / max
            select_cols.append(getattr(func, agg)(e).label(col.field))

    stmt = _base_stmt(spec.source, exprs, keys, select_cols=select_cols)
    for cond in where_conds:
        stmt = stmt.where(cond)
    group_exprs = [exprs[g] for g in group_keys]
    stmt = stmt.group_by(*group_exprs)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0

    for g in group_exprs:
        stmt = stmt.order_by(g.asc())
    cap = min(spec.limit or MAX_ROWS, MAX_ROWS)
    stmt = stmt.limit(cap)

    result = await db.execute(stmt)
    n = len(keys)
    rows = [
        {
            fd.key: _format_value(value, fd.type)
            for fd, value in zip(field_defs, row[:n], strict=True)
        }
        for row in result.all()
    ]
    out_columns = [
        {"key": fd.key, "header": col.header or fd.header, "type": fd.type, "editable": False}
        for col, fd in zip(columns, field_defs, strict=True)
    ]
    return TableResult(
        columns=out_columns, rows=rows, total=int(total),
        truncated=int(total) > len(rows),
    )


async def execute_spec(db: AsyncSession, spec: TableSpec) -> TableResult:
    """Compile the spec to one SQL query and return the FULL dataset."""
    # Ground the spec in the catalog first — a near-miss source/field name is
    # healed (not 422'd), keeping the agent honest. validate_spec runs before the
    # SOURCES lookup so an unresolvable source returns a helpful error, not KeyError.
    spec, _repairs = repair_spec_to_catalog(spec)
    problems = validate_spec(spec)
    if problems:
        raise ValueError("; ".join(problems))
    source = SOURCES[spec.source]

    if spec.source in VIRTUAL_SOURCES:
        return await _VIRTUAL_PROVIDERS[spec.source](db, spec)

    columns = list(spec.columns) or [
        ColumnSpec(field=f) for f in source.default_columns
    ]
    # Ensure group_by fields are visible columns (so the grouping is legible),
    # placed first. Skip unknown fields — validate_spec already vetted them.
    existing = {c.field for c in columns}
    for gb in spec.group_by:
        if gb in source.fields and gb not in existing:
            columns.insert(0, ColumnSpec(field=gb))
            existing.add(gb)
    keys = [c.field for c in columns]
    exprs = _EXPRS[spec.source]()
    stmt = _base_stmt(spec.source, exprs, keys)

    # Filters. Same-field filters OR together (facets: "фреза" OR "резец" on
    # description — a model/patch adding a second contains on the same field
    # almost always means "also include", never "and also contains both
    # substrings" which is an impossible/empty condition for distinct values).
    # Different fields AND together (narrowing), same as before.
    field_conds: dict[str, list[Any]] = {}
    primary_text = source.primary_text_field or "description"
    for flt in spec.filters:
        if flt.op == "smart":
            cond = await _smart_text_condition(db, spec.source, str(flt.value or ""))
            if cond is not None:
                # Smart filters operate on the primary text field. Group them with
                # same-field contains filters so a follow-up "добавь резцы" UNIONs
                # (фрезы OR резцы) instead of AND-ing to an empty result.
                field_conds.setdefault(primary_text, []).append(cond)
            continue
        expr = exprs[flt.field]
        cond = None
        if flt.op == "eq":
            cond = expr == flt.value
        elif flt.op == "ne":
            cond = expr != flt.value
        elif flt.op == "contains":
            cond = expr.ilike(f"%{flt.value}%")
        elif flt.op == "gte":
            cond = expr >= flt.value
        elif flt.op == "lte":
            cond = expr <= flt.value
        elif flt.op == "between":
            cond = expr.between(flt.value, flt.value2)
        elif flt.op == "in":
            values = flt.value if isinstance(flt.value, list) else [flt.value]
            cond = expr.in_(values)
        if cond is not None:
            field_conds.setdefault(flt.field, []).append(cond)
    where_conds: list[Any] = []
    for conds in field_conds.values():
        where_conds.append(conds[0] if len(conds) == 1 else or_(*conds))

    # Aggregating group_by: collapse to ONE row per group — the group key plus,
    # for each other column, a string_agg of distinct text values (e.g. all of a
    # supplier's milling items in one cell) or a SUM for numbers. "Выведи фрезы и
    # сгруппируй по поставщику" → two columns: поставщик | его фрезы.
    if spec.group_by and _can_aggregate(spec.source, keys):
        return await _execute_grouped(db, spec, source, columns, keys, exprs, where_conds)

    for cond in where_conds:
        stmt = stmt.where(cond)

    # True total BEFORE any limit — the full-data guarantee.
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0

    # Sort — group_by fields cluster first (primary keys), then explicit sort
    # applies within each group. "объедини по поставщикам, сортируй по дате" →
    # rows grouped under each supplier, date-sorted inside the group.
    explicit_sort_fields = {s.field for s in spec.sort}
    for gb in spec.group_by:
        if gb in exprs and gb not in explicit_sort_fields:
            stmt = stmt.order_by(exprs[gb].asc())
    for srt in spec.sort:
        expr = exprs[srt.field]
        stmt = stmt.order_by(expr.desc() if srt.dir == "desc" else expr.asc())

    cap = min(spec.limit or MAX_ROWS, MAX_ROWS)
    stmt = stmt.limit(cap)

    # Writable sources carry a hidden primary key per row so the grid can route
    # cell edits back to the right entity (through the approval gate).
    wb = WRITEBACK.get(spec.source)
    pk_expr = _pk_expr(spec.source)
    if pk_expr is not None:
        stmt = stmt.add_columns(pk_expr.label("__pk"))

    result = await db.execute(stmt)
    field_defs = [source.fields[k] for k in keys]
    n = len(keys)
    rows = []
    for row in result.all():
        data = {
            fd.key: _format_value(value, fd.type)
            for fd, value in zip(field_defs, row[:n], strict=True)
        }
        if pk_expr is not None:
            pk_val = row[n]
            data["__pk"] = str(pk_val) if pk_val is not None else None
        rows.append(data)

    editable = wb.editable if wb is not None else frozenset()
    out_columns = [
        {
            "key": fd.key,
            "header": col.header or fd.header,
            "type": fd.type,
            "editable": fd.key in editable,
        }
        for col, fd in zip(columns, field_defs, strict=True)
    ]
    return TableResult(
        columns=out_columns,
        rows=rows,
        total=int(total),
        truncated=int(total) > len(rows),
    )


# ── Patch operations ───────────────────────────────────────────────────────────


class PatchOp(BaseModel):
    op: Literal[
        "add_column", "remove_column", "move_column",
        "set_sort", "add_filter", "clear_filters", "set_limit",
        "set_group_by", "set_agg",
    ]
    field: str | None = None
    header: str | None = None
    before: str | None = None
    after: str | None = None
    dir: Literal["asc", "desc"] = "asc"
    filter: FilterSpec | None = None
    limit: int | None = None
    agg: Literal["sum", "avg", "min", "max", "count"] | None = None


def apply_patch(spec: TableSpec, ops: list[PatchOp]) -> TableSpec:
    """Pure spec transformation; raises ValueError on impossible ops."""
    source = SOURCES.get(spec.source)
    if source is None:
        raise ValueError(f"unknown source {spec.source!r}")
    spec = spec.model_copy(deep=True)

    def _index_of(key: str | None) -> int | None:
        if not key:
            return None
        for idx, col in enumerate(spec.columns):
            if col.field == key:
                return idx
        return None

    for op in ops:
        if op.op == "add_column":
            if not op.field or op.field not in source.fields:
                raise ValueError(f"unknown field {op.field!r}")
            if _index_of(op.field) is not None:
                continue  # idempotent
            col = ColumnSpec(field=op.field, header=op.header)
            if (idx := _index_of(op.before)) is not None:
                spec.columns.insert(idx, col)
            elif (idx := _index_of(op.after)) is not None:
                spec.columns.insert(idx + 1, col)
            else:
                spec.columns.append(col)
        elif op.op == "remove_column":
            idx = _index_of(op.field)
            if idx is None:
                raise ValueError(f"column {op.field!r} is not in the table")
            spec.columns.pop(idx)
        elif op.op == "move_column":
            idx = _index_of(op.field)
            if idx is None:
                raise ValueError(f"column {op.field!r} is not in the table")
            col = spec.columns.pop(idx)
            if (anchor := _index_of(op.before)) is not None:
                spec.columns.insert(anchor, col)
            elif (anchor := _index_of(op.after)) is not None:
                spec.columns.insert(anchor + 1, col)
            else:
                spec.columns.append(col)
        elif op.op == "set_sort":
            if not op.field or op.field not in source.fields:
                raise ValueError(f"unknown sort field {op.field!r}")
            spec.sort = [SortSpec(field=op.field, dir=op.dir)]
        elif op.op == "set_group_by":
            if not op.field or op.field not in source.fields:
                raise ValueError(f"unknown group_by field {op.field!r}")
            if op.field not in spec.group_by:
                spec.group_by = [op.field, *spec.group_by]
        elif op.op == "add_filter":
            if op.filter is None:
                raise ValueError("add_filter requires a filter")
            spec.filters.append(op.filter)
        elif op.op == "clear_filters":
            spec.filters = []
        elif op.op == "set_limit":
            spec.limit = op.limit
        elif op.op == "set_agg":
            if not op.field or op.field not in source.fields:
                raise ValueError(f"unknown agg field {op.field!r}")
            idx = _index_of(op.field)
            if idx is None:
                spec.columns.append(ColumnSpec(field=op.field, agg=op.agg))
            else:
                spec.columns[idx] = spec.columns[idx].model_copy(update={"agg": op.agg})
    return spec


# ── Deterministic NL command parser (0 LLM for recognised edits) ───────────────

_ADD_RE = re.compile(
    r"добав\w*\s+(?:столбец|столбц\w*|колонк\w*)\s+(?:с\s+|со\s+)?(?P<what>.+?)"
    r"(?:\s+(?P<pos>перед|после|до)\s+(?:столбц\w*\s+|колонк\w*\s+)?(?P<anchor>[\w\s]+?))?\s*$"
)
_REMOVE_RE = re.compile(
    r"(?:убери|убрать|удали|удалить)\s+(?:столбец|столбц\w*|колонк\w*)\s+(?:с\s+)?(?P<what>.+?)\s*$"
)
_SORT_RE = re.compile(
    r"(?:отсортируй|сортируй|сортировка|упорядочь?и?)\s+(?:по\s+)?(?P<what>[\w\s]+?)"
    r"(?:\s+(?P<dir>по\s+убыван\w*|по\s+возрастан\w*|убыв\w*|возраст\w*))?\s*$"
)
_ONLY_RE = re.compile(
    r"(?:покажи|выведи|оставь)\s+только\s+(?P<what>.+?)\s*$"
)
# Grouping directive anywhere in the request: «объедини по поставщикам»,
# «сгруппируй по поставщику», «группировка по дате».
_GROUP_RE = re.compile(
    r"(?:объедин\w+|сгруппир\w+|группир\w+|группиров\w+)\s+по\s+(?P<what>[\w]+(?:\s+[\w]+){0,2})"
)
# Sort directive anywhere in the request (not anchored to the whole string).
_SORT_INLINE_RE = re.compile(
    r"(?:отсортир\w+|сортир\w+|упорядоч\w+)\s+(?:по\s+)?(?P<what>[\w]+(?:\s+[\w]+){0,2}?)"
    r"(?:\s+(?P<dir>по\s+убыван\w*|по\s+возрастан\w*|убыв\w*|возраст\w*))?"
)
# Filter-add continuation: "и пластины", "а также резцы", "добавь болты М8".
# Fired when the user extends the current filter set with new item(s).
# Must be anchored (match from start) so "и почему так?" doesn't qualify.
_AND_FILTER_RE = re.compile(
    r"^(?:и\s+|а\s+также\s+|добавь\s+(?:к\s+(?:ним|нему|таблице)\s+)?(?:все(?:х|м)?\s+)?|включи\s+(?:все(?:х)?\s+)?)"
    r"(?P<what>[а-яёa-z][а-яёa-z0-9\s\-]{1,50}?)\s*$",
    re.IGNORECASE,
)
# Words that mean the user is asking a question/making a statement, not adding an item.
_NON_ITEM_WORDS = frozenset({
    "почему", "зачем", "когда", "куда", "откуда", "где", "как", "так",
    "правда", "хорошо", "плохо", "верно", "неверно", "ясно", "конечно",
    "они", "вы", "ты", "он", "она", "мы", "нас", "вас",
})


def _contains_stem(word: str) -> str:
    """Strip common Russian case/plural endings to get an ilike-friendly stem.

    "пластины" → "пластин", "фрезы" → "фрез", "резцы" → "резц".
    Keeps at least 4 chars so short words are not mangled.
    """
    word = word.strip()
    if len(word) <= 4:
        return word
    for ending in ("ями", "ами", "ях", "ах", "ов", "ей", "ам", "ям"):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[:-len(ending)]
    for ending in ("ые", "ий", "ие", "ых", "их"):
        if word.endswith(ending) and len(word) - 2 >= 4:
            return word[:-2]
    for ending in ("ы", "и", "е", "а", "я"):
        if word.endswith(ending) and len(word) - 1 >= 4:
            return word[:-1]
    return word


def _filter_items_from_query(query: str) -> list[str]:
    """Split a query on ' и ' and return stems for each item part.

    "резцы и пластины" → ["резц", "пластин"].
    Single-item queries return a one-element list.
    Numbers and qualifier words are kept as-is; the overall split is on the
    Russian conjunction 'и' used as an item separator.
    """
    parts = [p.strip() for p in re.split(r"\s+и\s+", query, flags=re.IGNORECASE) if p.strip()]
    stems: list[str] = []
    for part in parts:
        # Take only the last significant word of each part as the contains-stem
        # (strips pre-qualifiers like "все", "любые").
        words = [w for w in part.split() if w not in _STOP_TOKENS and len(w) >= 3]
        if not words:
            continue
        # Use the full part stem when it's a short phrase (≤ 2 content words).
        if len(words) <= 2:
            stem = _contains_stem(" ".join(words))
        else:
            stem = _contains_stem(part)
        if len(stem) >= 3:
            stems.append(stem)
    return stems


@dataclass
class ParsedCommand:
    ops: list[PatchOp] = dc_field(default_factory=list)
    description: str = ""


def parse_patch_command(text: str, spec: TableSpec) -> ParsedCommand | None:
    """Recognise a table-edit command deterministically, or None → use the LLM.

    Supported: добавить/убрать столбец (с позицией «перед/после X»),
    сортировка, «покажи только …», «и X / а также X / добавь X» (расширение фильтра).
    """
    source = SOURCES.get(spec.source)
    if source is None:
        return None
    t = _norm(text)

    if m := _ADD_RE.search(t):
        fd = resolve_field(source, m.group("what"))
        if fd is None:
            return None
        anchor_fd = resolve_field(source, m.group("anchor") or "")
        pos = m.group("pos")
        op = PatchOp(
            op="add_column",
            field=fd.key,
            before=anchor_fd.key if (anchor_fd and pos in ("перед", "до")) else None,
            after=anchor_fd.key if (anchor_fd and pos == "после") else None,
        )
        return ParsedCommand([op], f"добавил столбец «{fd.header}»")

    if m := _REMOVE_RE.search(t):
        fd = resolve_field(source, m.group("what"))
        if fd is None:
            return None
        return ParsedCommand(
            [PatchOp(op="remove_column", field=fd.key)],
            f"убрал столбец «{fd.header}»",
        )

    if m := _SORT_RE.search(t):
        fd = resolve_field(source, m.group("what"))
        if fd is None:
            return None
        direction = "desc" if "убыв" in (m.group("dir") or "") else "asc"
        return ParsedCommand(
            [PatchOp(op="set_sort", field=fd.key, dir=direction)],
            f"отсортировал по «{fd.header}» ({'убыв.' if direction == 'desc' else 'возр.'})",
        )

    target_field = source.primary_text_field or "description"

    if m := _ONLY_RE.search(t):
        query = m.group("what").strip()
        items = _filter_items_from_query(query)
        if len(items) > 1:
            # "оставь только резцы и пластины" → clear + one contains per item.
            # OR-semantics come from execute_spec's same-field grouping: adding
            # multiple contains on the same field produces OR, not AND.
            ops_list: list[PatchOp] = [PatchOp(op="clear_filters")]
            for stem in items:
                ops_list.append(PatchOp(op="add_filter", filter=FilterSpec(
                    field=target_field, op="contains", value=stem,
                )))
            return ParsedCommand(ops_list, f"оставил только «{query}»")
        # Single item → keep smart filter (FTS + stemming).
        return ParsedCommand(
            [
                PatchOp(op="clear_filters"),
                PatchOp(op="add_filter", filter=FilterSpec(
                    field=target_field, op="smart", value=query,
                )),
            ],
            f"оставил только «{query}»",
        )

    if m := _AND_FILTER_RE.match(t):
        what = m.group("what").strip()
        # Reject if the matched text looks like a question/statement, not an item.
        words = what.split()
        if any(w in _NON_ITEM_WORDS for w in words):
            return None
        items = _filter_items_from_query(what)
        if not items:
            return None
        ops_list = [
            PatchOp(op="add_filter", filter=FilterSpec(
                field=target_field, op="contains", value=stem,
            ))
            for stem in items
        ]
        label = " и ".join(f"«{s}»" for s in items)
        return ParsedCommand(ops_list, f"добавил фильтр по {label}")

    return None


# Grouping synonyms beyond «сгруппируй/объедини по X»: «в разрезе поставщиков»,
# «по каждому поставщику».
_GROUP_RE2 = re.compile(
    r"(?:в\s+разрезе|по\s+кажд\w+|разрез\w*\s+по)\s+(?P<what>[\w]+(?:\s+[\w]+){0,2})"
)
# Bare «по X» / «в разрезе X» — counted as grouping ONLY under an aggregate or
# comparison intent (otherwise "по" is too ambiguous to mean grouping).
_GROUP_BARE_RE = re.compile(r"(?:в\s+разрезе|по)\s+(?P<what>[\w-]+)")
# «… по <dim>» at the END of the request («фрезы по поставщику») — a grouping
# even without «сгруппируй»/agg, but only when <dim> is a categorical/date
# dimension. End-anchored so «по поставщику ИНАТЕК» (a filter) does NOT match.
_GROUP_TAIL_RE = re.compile(r"\bпо\s+(?P<what>[A-Za-zА-Яа-яЁё-]+)\s*$")


def _is_dimension(source: SourceDef, fd: FieldDef) -> bool:
    """A low-cardinality grouping dimension (supplier/date/status/…), not the
    item description or a numeric measure."""
    return fd.type in ("text", "date") and fd.key != source.primary_text_field


def _resolve_noun(source: SourceDef, raw: str) -> FieldDef | None:
    """resolve_field with a plural/genitive fallback («поставщиков» → «поставщик»)."""
    fd = resolve_field(source, raw)
    if fd:
        return fd
    raw = (raw or "").strip()
    for suf in ("ами", "ями", "ов", "ев", "ах", "ям", "ам", "ей", "и", "ы", "у", "е", "а"):
        if raw.endswith(suf) and len(raw) - len(suf) >= 4:
            fd = resolve_field(source, raw[: -len(suf)])
            if fd:
                return fd
    return None

# Aggregate-function intent → applied to a numeric column under grouping.
_AGG_LABEL = {"avg": "среднее", "min": "минимум", "max": "максимум",
              "sum": "сумма", "count": "количество"}
_AGG_RES: list[tuple[str, re.Pattern]] = [
    ("avg", re.compile(r"средн|в\s+среднем|сравн\w*\s+цен")),
    ("min", re.compile(r"минимальн|наименьш|дешевл|дешёв|деше\w*\s+всег")),
    ("max", re.compile(r"максимальн|наибольш|дорож|дорог")),
    ("count", re.compile(r"скольк\w+|количеств\w+|число\s+поз")),
]
_PRIMARY_NUMBER_FIELD = {
    "invoice_items": "unit_price", "invoices": "total_amount",
    "payments": "amount", "warehouse": "current_qty",
}


# «выведи ВСЕ ФРЕЗЫ по поставщику» — the object between the verb and «по/за/…».
_ITEM_OBJ_RE = re.compile(
    r"(?:покажи|показать|выведи|вывести|выбери|отбери|дай|нужн\w+)\s+"
    r"(?:все\s+|весь\s+|вся\s+|всю\s+|мне\s+|нам\s+)*"
    r"(?P<obj>[\wё][\wё\s-]*?)\s+(?:по\s|за\s|с\s|из\s|от\s|у\s|в\s+разрезе)"
)


def _recover_item_filter(source: SourceDef, text: str, spec: TableSpec) -> str | None:
    """Recover the item the user filtered on but the worker dropped («выведи
    фрезы по поставщику» built without the фрезы filter). Conservative: only
    item-bearing sources, only when NO text filter is present, and never the
    source's own name («покажи позиции по…»)."""
    if source.key not in ("invoice_items", "warehouse"):
        return None
    ptf = source.primary_text_field
    if not ptf:
        return None
    if any((f.op == "smart") or (f.field == ptf and f.op == "contains") for f in spec.filters):
        return None
    m = _ITEM_OBJ_RE.search(text)
    if not m:
        return None
    obj = " ".join(
        w for w in m.group("obj").split() if w not in _STOP_TOKENS and len(w) >= 3
    ).strip()
    if len(obj) < 4:
        return None
    low = obj.lower()
    # Never filter by a source-type word («счета», «документы», «позиции»…) —
    # that's the dataset, not an item within it.
    source_words = {w.lower() for src in SOURCES.values()
                    for w in (src.key, src.title, *src.synonyms)}
    if any(low == w or w in low for w in source_words):
        return None
    return obj


def _detect_agg_intent(text: str) -> str | None:
    for agg, rx in _AGG_RES:
        if rx.search(text):
            return agg
    return None


def _primary_number_field(source: SourceDef, text: str) -> FieldDef | None:
    """Numeric field the aggregate targets — explicit noun first, else per-source
    default (цена/сумма), else the first numeric field."""
    for noun in ("цена", "цены", "цену", "сумма", "суммы", "стоимость",
                 "количество", "кол-во"):
        if noun in text:
            fd = resolve_field(source, noun)
            if fd and fd.type == "number":
                return fd
    key = _PRIMARY_NUMBER_FIELD.get(source.key)
    if key and key in source.fields:
        return source.fields[key]
    return next((fd for fd in source.fields.values() if fd.type == "number"), None)


def reconcile_ops(spec: TableSpec, user_text: str) -> tuple[list[PatchOp], list[str]]:
    """Deterministically derive grouping/sort the user asked for but the worker
    LLM may have dropped from a multi-clause request.

    Scans the ORIGINAL request for «объедини/сгруппируй по X» and «сортируй по X»
    and returns the PatchOps needed to add the missing group_by / sort — so a
    complex query is honoured structurally, not left to the model's compliance.
    Returns ([] , []) when the spec already satisfies the request. Idempotent.
    """
    source = SOURCES.get(spec.source)
    if source is None:
        return [], []
    text = _norm(user_text)
    ops: list[PatchOp] = []
    notes: list[str] = []

    agg = _detect_agg_intent(text)
    gm = _GROUP_RE.search(text) or _GROUP_RE2.search(text)
    group_fd = _resolve_noun(source, gm.group("what")) if gm else None
    # Contextual bare grouping: «сравни цены по поставщику» — only when there is an
    # aggregate/comparison intent, so an unrelated «по» never forces grouping.
    if group_fd is None and (agg or "сравн" in text):
        bm = _GROUP_BARE_RE.search(text)
        if bm:
            group_fd = _resolve_noun(source, bm.group("what"))
    # «фрезы по поставщику» — trailing «по <dimension>» is a grouping even without
    # an explicit «сгруппируй»/aggregate, when the field is a real dimension.
    if group_fd is None:
        tm = _GROUP_TAIL_RE.search(text)
        if tm:
            cand = _resolve_noun(source, tm.group("what"))
            if cand and _is_dimension(source, cand):
                group_fd = cand
    if group_fd and group_fd.key not in spec.group_by:
        ops.append(PatchOp(op="set_group_by", field=group_fd.key))
        notes.append(f"группировка по «{group_fd.header}»")

    sm = _SORT_INLINE_RE.search(text)
    if sm:
        fd = resolve_field(source, sm.group("what"))
        if fd and not any(s.field == fd.key for s in spec.sort):
            dir_str = sm.group("dir") or ""
            direction = "desc" if "убыв" in dir_str else "asc"
            ops.append(PatchOp(op="set_sort", field=fd.key, dir=direction))
            notes.append(f"сортировка по «{fd.header}»")

    # Aggregate-function intent (средняя/минимальная/максимальная цена, сравни
    # цены, сколько). Only meaningful when the table is — or becomes — grouped.
    grouped = bool(spec.group_by) or any(o.op == "set_group_by" for o in ops)
    if agg and grouped:
        num_fd = _primary_number_field(source, text)
        if num_fd and not any(
            c.field == num_fd.key and c.agg == agg for c in spec.columns
        ):
            ops.append(PatchOp(op="set_agg", field=num_fd.key, agg=agg))
            notes.append(f"«{num_fd.header}» — {_AGG_LABEL[agg]}")

    # Recover a dropped item filter, but only for a grouped «<item> по <dim>»
    # request — the clean case where the item clearly precedes «по».
    if grouped:
        item = _recover_item_filter(source, text, spec)
        if item:
            ops.append(PatchOp(op="add_filter", filter=FilterSpec(
                field=source.primary_text_field or "description", op="smart", value=item)))
            notes.append(f"фильтр по «{item}»")

    return ops, notes
