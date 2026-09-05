"""Человекочитаемое описание действия, которое агент просит подтвердить.

До этого модуля запрос подтверждения показывал ``json.dumps(args)``: для
отправки письма человек видел ``{"action":"send","draft_id":"7f3a…",
"expected_digest":"9c2b…"}`` — то есть утверждал идентификаторы, а не письмо.
Прочитать то, что сейчас уйдёт наружу, в момент решения было негде, и гейт
превращался в формальность: две кнопки под нечитаемой строкой.

Здесь собирается карточка: заголовок («Отправить письмо»), поля («Кому»,
«Тема», «Из ящика», «Вложения»), тело письма и предупреждения проверки
рисков. Данные берутся из БД по идентификаторам, потому что аргументы вызова
их и содержат — id, а не содержимое.

Карточка НЕ заменяет проверок: она только показывает. Всё, что решает, можно
ли действие, живёт в policy_engine, email_risk и gate_actions.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()


@dataclass
class PreviewField:
    label: str
    value: str
    #: «сильное» поле выделяется в интерфейсе (получатель, сумма)
    emphasis: bool = False


@dataclass
class ApprovalPreview:
    """Что именно предлагается сделать — словами, а не аргументами вызова."""

    title: str
    #: Уточнение под заголовком: поставщик, ящик, номер счёта.
    subtitle: str | None = None
    fields: list[PreviewField] = field(default_factory=list)
    #: Тело письма/сообщения, если действие что-то отправляет наружу.
    body_html: str | None = None
    body_text: str | None = None
    #: Предупреждения (не блокирующие) — их человек тоже должен видеть ДО «да».
    warnings: list[str] = field(default_factory=list)
    #: Зачем агент это делает (G3 reason), если он его назвал.
    reason: str | None = None
    #: Что именно правится, если карточка редактируемая (пока только письмо).
    editable: str | None = None
    #: Идентификатор редактируемой сущности (черновик письма).
    entity_id: str | None = None
    #: Необратимость: такие действия не подпадают под «подтвердить всё».
    irreversible: bool = False
    #: Полные аргументы — «показать как есть» для тех, кому нужно.
    raw_args: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _plain(html: str | None, text: str | None) -> str:
    import re

    if text and text.strip():
        return text.strip()
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ").strip()


# Действия, которые нельзя отменить: письмо ушло, деньги отмечены, файл удалён.
# Для них «подтвердить всё» не действует — решение принимается по каждому.
IRREVERSIBLE_ACTIONS = {
    "send",
    "send_rfq",
    "mark_paid",
    "delete",
    "bulk_delete",
    "delete_template",
    "approve",
    "reject",
    "resolve",
    "export_1c",
    "issue_stock",
    "promote",
}


def is_irreversible(skill_name: str, args: dict) -> bool:
    action = str(args.get("action") or "").strip()
    if action in IRREVERSIBLE_ACTIONS:
        return True
    tail = skill_name.rsplit(".", 1)[-1]
    return tail in IRREVERSIBLE_ACTIONS


async def build_preview(skill_name: str, args: dict, db=None) -> ApprovalPreview:
    """Карточка подтверждения для одного вызова.

    ``db`` — уже открытая сессия, если она есть у вызывающего; иначе модуль
    откроет свою. Агентский цикл сессии не держит, а вызывающие изнутри
    запроса (и тесты) держат — и их транзакция чужой сессии не видна.

    Никогда не бросает: сломанное превью не должно мешать самому гейту — в
    худшем случае человек увидит аргументы списком, а не письмом.
    """
    args = args or {}
    reason = args.get("reason") if isinstance(args.get("reason"), str) else None
    preview: ApprovalPreview | None = None
    try:
        preview = await _build(skill_name, args, db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("approval_preview_failed", skill=skill_name, error=str(exc))
    if preview is None:
        preview = _generic(skill_name, args)
    preview.reason = preview.reason or reason
    preview.irreversible = is_irreversible(skill_name, args)
    preview.raw_args = {k: v for k, v in args.items() if k != "reason"}
    return preview


def _generic(skill_name: str, args: dict) -> ApprovalPreview:
    """Запасной вариант: аргументы парами «поле — значение», а не JSON-дамп."""
    action = str(args.get("action") or "").strip()
    fields = [
        PreviewField(label=k, value=str(v)[:300])
        for k, v in args.items()
        if k not in ("action", "reason") and v not in (None, "", [], {})
    ]
    return ApprovalPreview(
        title=f"{skill_name}: {action}" if action else skill_name,
        fields=fields[:12],
    )


import contextlib


@contextlib.asynccontextmanager
async def _session(db=None):
    """Готовая сессия вызывающего либо своя собственная."""
    if db is not None:
        yield db
        return
    from app.db.session import _get_session_factory

    async with _get_session_factory()() as own:
        yield own


async def _build(skill_name: str, args: dict, db=None) -> ApprovalPreview | None:
    capability = skill_name.split(".")[0]
    action = str(args.get("action") or skill_name.rsplit(".", 1)[-1]).strip()

    if capability == "email" and action == "send":
        return await _email_send(args, db)
    if capability == "invoices" and action in ("approve", "reject"):
        return await _invoice_decision(args, action, db)
    if capability == "payments" and action == "mark_paid":
        return await _payment(args, db)
    if capability == "procurement" and action == "send_rfq":
        return await _rfq(args, db)
    return None


async def _email_send(args: dict, db=None) -> ApprovalPreview | None:
    """Письмо целиком: кому, о чём, из какого ящика и что в теле."""
    from sqlalchemy import select

    from app.db.models import DraftAction, EmailAttachment

    draft_id = _uuid(args.get("draft_id") or args.get("id"))
    if draft_id is None:
        return None

    async with _session(db) as db:
        draft = await db.get(DraftAction, draft_id)
        if draft is None:
            return None
        data = draft.draft_data or {}

        names: list[str] = []
        raw_ids = [i for i in (_uuid(a) for a in data.get("attachment_ids") or []) if i]
        if raw_ids:
            rows = (
                (
                    await db.execute(
                        select(EmailAttachment.filename).where(EmailAttachment.id.in_(raw_ids))
                    )
                )
                .scalars()
                .all()
            )
            names = [n for n in rows if n]

    to = [a for a in (data.get("to_addresses") or []) if a]
    cc = [a for a in (data.get("cc_addresses") or []) if a]
    fields = [PreviewField(label="Кому", value=", ".join(to) or "—", emphasis=True)]
    if cc:
        fields.append(PreviewField(label="Копия", value=", ".join(cc)))
    fields.append(PreviewField(label="Тема", value=data.get("subject") or "(без темы)"))
    fields.append(PreviewField(label="Из ящика", value=data.get("mailbox") or "—"))
    if names:
        fields.append(PreviewField(label="Вложения", value=", ".join(names)))

    # Предупреждения проверки рисков — те самые, что человек обязан увидеть
    # ДО решения, а не после.
    warnings = [
        str(f.get("message"))
        for f in (data.get("risk_flags") or [])
        if f.get("severity") in ("warning", "error") and f.get("message")
    ]
    acknowledged = args.get("acknowledged_risks") or []
    if acknowledged:
        warnings.append(
            "Агент предлагает принять риски: " + ", ".join(str(c) for c in acknowledged)
        )

    return ApprovalPreview(
        title="Отправить письмо",
        subtitle=data.get("subject") or None,
        fields=fields,
        body_html=data.get("body_html"),
        body_text=_plain(data.get("body_html"), data.get("body_text"))[:4000],
        warnings=warnings,
        editable="email_draft",
        entity_id=str(draft_id),
    )


async def _invoice_decision(args: dict, action: str, db=None) -> ApprovalPreview | None:
    from app.db.models import Invoice

    invoice_id = _uuid(args.get("invoice_id") or args.get("id"))
    if invoice_id is None:
        return None
    async with _session(db) as db:
        inv = await db.get(Invoice, invoice_id)
        if inv is None:
            return None
        supplier = None
        if inv.supplier_id:
            from app.db.models import Party

            party = await db.get(Party, inv.supplier_id)
            supplier = party.name if party else None
        number = inv.invoice_number
        total = inv.total_amount
        currency = inv.currency
        due = inv.due_date

    fields = [
        PreviewField(label="Поставщик", value=supplier or "—", emphasis=True),
        PreviewField(
            label="Сумма",
            value=f"{total} {currency or ''}".strip() if total is not None else "—",
            emphasis=True,
        ),
        PreviewField(label="Срок оплаты", value=due.date().isoformat() if due else "—"),
    ]
    return ApprovalPreview(
        title="Утвердить счёт" if action == "approve" else "Отклонить счёт",
        subtitle=f"№ {number}" if number else None,
        fields=fields,
    )


async def _payment(args: dict, db=None) -> ApprovalPreview | None:
    from app.db.models import PaymentSchedule

    schedule_id = _uuid(args.get("schedule_id") or args.get("id"))
    if schedule_id is None:
        return None
    async with _session(db) as db:
        row = await db.get(PaymentSchedule, schedule_id)
        if row is None:
            return None
        amount = row.amount
        currency = getattr(row, "currency", None)
        due = row.due_date

    return ApprovalPreview(
        title="Отметить платёж выполненным",
        fields=[
            PreviewField(
                label="Сумма",
                value=f"{amount} {currency or ''}".strip() if amount is not None else "—",
                emphasis=True,
            ),
            PreviewField(label="Срок", value=due.isoformat() if due else "—"),
        ],
    )


async def _rfq(args: dict, db=None) -> ApprovalPreview | None:
    from app.db.models import PurchaseRequest

    request_id = _uuid(args.get("request_id") or args.get("id"))
    if request_id is None:
        return None
    async with _session(db) as db:
        req = await db.get(PurchaseRequest, request_id)
        if req is None:
            return None
        title = req.title
        items = len(req.items or []) if hasattr(req, "items") else None

    suppliers = args.get("supplier_ids") or args.get("suppliers") or []
    fields = [PreviewField(label="Заявка", value=title or "—", emphasis=True)]
    if items is not None:
        fields.append(PreviewField(label="Позиций", value=str(items)))
    if suppliers:
        fields.append(PreviewField(label="Поставщиков", value=str(len(suppliers)), emphasis=True))
    return ApprovalPreview(title="Разослать запрос коммерческих предложений", fields=fields)


# ── Короткое описание вызова (для плана хода) ──────────────────────────────

_ACTION_VERBS = {
    "list": "посмотрю список",
    "search": "поищу",
    "get": "открою",
    "read": "прочитаю",
    "draft": "подготовлю черновик",
    "compose": "составлю письмо",
    "reply": "подготовлю ответ",
    "send": "отправлю письмо",
    "risk_check": "проверю риски",
    "approve": "утвержу",
    "reject": "отклоню",
    "validate": "проверю",
    "extract": "извлеку данные",
    "classify": "определю тип",
    "export_excel": "выгружу в Excel",
    "export_1c": "выгружу в 1С",
    "spec_table": "соберу таблицу",
    "mark_paid": "отмечу оплату",
    "search_positions": "поищу позиции в каталоге",
    "search_visual": "поищу по изображению",
    "fetch_new": "проверю новую почту",
}

_CAPABILITY_NOUNS = {
    "email": "в почте",
    "invoices": "по счетам",
    "documents": "по документам",
    "suppliers": "по поставщикам",
    "payments": "по платежам",
    "anomalies": "по аномалиям",
    "tool_catalog": "в каталогах",
    "workspace": "на рабочем столе",
    "memory": "в памяти",
    "analytics": "в аналитике",
}


def describe_call(skill_name: str, args: dict) -> str:
    """Человеческая формулировка одного шага: «отправлю письмо в почте».

    Нужна для плана, который агент показывает ДО выполнения: список имён
    инструментов («email», «invoices») ничего не говорит о намерении, а по
    факту выполнения план уже поздно поправлять.
    """
    capability = skill_name.split(".")[0]
    action = str((args or {}).get("action") or skill_name.rsplit(".", 1)[-1]).strip()
    verb = _ACTION_VERBS.get(action)
    noun = _CAPABILITY_NOUNS.get(capability)
    if verb and noun:
        return f"{verb} {noun}"
    if verb:
        return verb
    return f"{capability}: {action}" if action else capability
