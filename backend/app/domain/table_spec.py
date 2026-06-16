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
from sqlalchemy import Select, Text, and_, case, cast, func, literal, or_, select
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
)

logger = structlog.get_logger()

MAX_ROWS = 5000  # hard cap on rendered rows; `total` is always the true count


# ── Spec models ────────────────────────────────────────────────────────────────


class ColumnSpec(BaseModel):
    field: str
    header: str | None = None

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
        return str(value).strip().lower() if isinstance(value, str) else value


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
}


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


def _items_list_subq():
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


def _base_stmt(source_key: str, exprs: dict[str, Any], keys: list[str]) -> Select:
    cols = [exprs[k].label(k) for k in keys]
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
        return select(*cols).select_from(Document)
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
    return problems


async def execute_spec(db: AsyncSession, spec: TableSpec) -> TableResult:
    """Compile the spec to one SQL query and return the FULL dataset."""
    source = SOURCES[spec.source]
    problems = validate_spec(spec)
    if problems:
        raise ValueError("; ".join(problems))

    columns = list(spec.columns) or [
        ColumnSpec(field=f) for f in source.default_columns
    ]
    keys = [c.field for c in columns]
    exprs = _EXPRS[spec.source]()
    stmt = _base_stmt(spec.source, exprs, keys)

    # Filters. Same-field filters OR together (facets: "фреза" OR "резец" on
    # description — a model/patch adding a second contains on the same field
    # almost always means "also include", never "and also contains both
    # substrings" which is an impossible/empty condition for distinct values).
    # Different fields AND together (narrowing), same as before.
    field_conds: dict[str, list[Any]] = {}
    smart_conds: list[Any] = []
    for flt in spec.filters:
        if flt.op == "smart":
            cond = await _smart_text_condition(db, spec.source, str(flt.value or ""))
            if cond is not None:
                smart_conds.append(cond)
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
    for conds in field_conds.values():
        stmt = stmt.where(conds[0] if len(conds) == 1 else or_(*conds))
    for cond in smart_conds:
        stmt = stmt.where(cond)

    # True total BEFORE any limit — the full-data guarantee.
    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar() or 0

    # Sort
    for srt in spec.sort:
        expr = exprs[srt.field]
        stmt = stmt.order_by(expr.desc() if srt.dir == "desc" else expr.asc())

    cap = min(spec.limit or MAX_ROWS, MAX_ROWS)
    stmt = stmt.limit(cap)

    result = await db.execute(stmt)
    field_defs = [source.fields[k] for k in keys]
    rows = [
        {
            fd.key: _format_value(value, fd.type)
            for fd, value in zip(field_defs, row, strict=True)
        }
        for row in result.all()
    ]

    out_columns = [
        {
            "key": fd.key,
            "header": col.header or fd.header,
            "type": fd.type,
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
    ]
    field: str | None = None
    header: str | None = None
    before: str | None = None
    after: str | None = None
    dir: Literal["asc", "desc"] = "asc"
    filter: FilterSpec | None = None
    limit: int | None = None


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
        elif op.op == "add_filter":
            if op.filter is None:
                raise ValueError("add_filter requires a filter")
            spec.filters.append(op.filter)
        elif op.op == "clear_filters":
            spec.filters = []
        elif op.op == "set_limit":
            spec.limit = op.limit
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


@dataclass
class ParsedCommand:
    ops: list[PatchOp] = dc_field(default_factory=list)
    description: str = ""


def parse_patch_command(text: str, spec: TableSpec) -> ParsedCommand | None:
    """Recognise a table-edit command deterministically, or None → use the LLM.

    Supported: добавить/убрать столбец (с позицией «перед/после X»),
    сортировка, «покажи только …» (smart-фильтр по содержимому).
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

    if m := _ONLY_RE.search(t):
        query = m.group("what")
        return ParsedCommand(
            [
                PatchOp(op="clear_filters"),
                PatchOp(op="add_filter", filter=FilterSpec(
                    field=source.primary_text_field or "description",
                    op="smart",
                    value=query,
                )),
            ],
            f"оставил только «{query}»",
        )

    return None
