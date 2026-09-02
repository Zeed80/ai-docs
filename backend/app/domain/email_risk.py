"""Pre-send risk detectors — one implementation, both send paths.

Until Ф4 these lived inside the ``email.risk_check`` endpoint and therefore
protected only the AGENT. A person hitting Send went through
``POST /api/email/send``, which stamped the draft ``approved`` and dispatched
it — so the path with the higher error rate had no checks at all, while the
one with an approval gate had all of them.

The other half of the old design: ``send_email`` blocked only flags with
``severity="error"`` AND ``can_override is False``, and nothing ever produced
that combination, so the single error-level detector could not stop anything.
Blocking is now an explicit, configurable decision (``BLOCKING_CODES``), not an
accident of two flags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Codes that stop a send until a human explicitly overrides it. Everything else
# is advisory: "внешний домен" is the normal case for supplier mail and must
# not turn into a wall.
BLOCKING_CODES = frozenset({"sensitive_content", "lookalike_domain"})


@dataclass
class RiskFlagData:
    code: str
    severity: str          # "info" | "warning" | "error"
    message: str
    can_override: bool = True
    details: dict = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.code in BLOCKING_CODES


_ATTACHMENT_WORDS = (
    "во вложении", "прилагаю", "прилагаем", "в приложении", "см. вложение",
    "смотрите вложение", "attached", "attachment", "вложение:",
)

# Деньги — это ЧИСЛО с валютой, а не слово «руб» где-то в тексте. Подстрочный
# список ("руб", "сумм", "оплат") помечал предупреждением почти каждое
# коммерческое письмо, и предупреждения перестали читать.
_AMOUNT_RE = re.compile(
    r"\d[\d  .,]*\s*(?:руб|₽|rub|usd|eur|\$|€|тыс|млн)"
    r"|(?:сумма|к оплате|итого|предоплат\w*)\D{0,20}\d",
    re.IGNORECASE,
)

# Чувствительное содержание. Границы слов обязательны: подстрока «секрет»
# ловила «секретарь» и «секретариат» — самые частые слова деловой переписки, а
# срабатывание здесь БЛОКИРУЕТ отправку (см. BLOCKING_CODES).
_SENSITIVE_RE = re.compile(
    r"конфиденциальн\w*"
    r"|\bсекретн\w*|\bсовершенно секретно\b|\bпод грифом\b"
    r"|не для распростран\w*"
    r"|коммерческ\w*\s+тайн\w*",
    re.IGNORECASE,
)

# Начало цитируемой части письма. Дисклеймер «письмо конфиденциально» живёт в
# подписи входящего письма и попадает в цитату КАЖДОГО ответа — детекторы
# должны смотреть на то, что человек написал сам, а не на историю переписки.
_QUOTE_MARKERS = (
    "-----original message",
    "-----исходное сообщение",
    "________________________________",
    "> ",
)
_QUOTE_LINE_RE = re.compile(
    r"^\s*(?:>|(?:\d{1,2}[.:/ ].{0,40})?(?:написал|wrote)\s*:|"
    r"(?:от|from|отправлено|sent)\s*:\s*.+)",
    re.IGNORECASE,
)


def strip_quoted(body: str) -> str:
    """Только новая часть письма, без процитированной переписки.

    Прагматично, а не идеально: обрезаем по первому маркеру цитаты. Если
    маркеров нет, текст возвращается целиком.
    """
    text = body or ""
    lowered = text.lower()
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        pos = lowered.find(marker)
        if pos != -1:
            cut = min(cut, pos)
    lines = text[:cut].splitlines()
    kept: list[str] = []
    for line in lines:
        if _QUOTE_LINE_RE.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def _domain(addr: str) -> str:
    return addr.split("@")[-1].strip().lower() if "@" in (addr or "") else ""


def levenshtein(a: str, b: str) -> int:
    """Small pure-Python edit distance — inputs here are domain names."""
    if a == b:
        return 0
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def detect_lookalike_domain(recipients: list[str], known: set[str]) -> RiskFlagData | None:
    """A recipient domain one or two edits away from a domain we know.

    ``romex.example`` vs ``rornex.example`` is invisible at a glance and is how
    invoice-redirection fraud actually lands.
    """
    for addr in recipients:
        domain = _domain(addr)
        if not domain or domain in known:
            continue
        for candidate in known:
            if not candidate or abs(len(candidate) - len(domain)) > 2:
                continue
            distance = levenshtein(domain, candidate)
            if 0 < distance <= 2:
                return RiskFlagData(
                    code="lookalike_domain",
                    severity="error",
                    message=(
                        f"Домен получателя «{domain}» почти совпадает с известным "
                        f"«{candidate}» — проверьте адрес"
                    ),
                    details={"domain": domain, "looks_like": candidate},
                )
    return None


def detect_promised_attachment(body: str, attachment_count: int) -> RiskFlagData | None:
    """"Во вложении счёт" with nothing attached."""
    if attachment_count:
        return None
    lowered = strip_quoted(body).lower()
    if any(word in lowered for word in _ATTACHMENT_WORDS):
        return RiskFlagData(
            code="promised_attachment_missing",
            severity="warning",
            message="В тексте упомянуто вложение, но к письму ничего не приложено",
        )
    return None


def detect_amount_without_context(body: str, context: dict | None) -> RiskFlagData | None:
    if not _AMOUNT_RE.search(strip_quoted(body)):
        return None
    ctx = context or {}
    if ctx.get("invoice_id") or ctx.get("document_id"):
        return None
    return RiskFlagData(
        code="amount_no_attachment",
        severity="warning",
        message="Упомянута сумма/оплата, но нет привязки к документу",
    )


def detect_sensitive_content(body: str) -> RiskFlagData | None:
    """Только по НОВОЙ части письма и только по целым словам.

    Оба сужения — из-за того, что этот код блокирует отправку: по подстроке
    «секрет» стеной становился любой ответ секретарю, а по цитате — любой
    ответ на письмо с дисклеймером о конфиденциальности в подписи.
    """
    match = _SENSITIVE_RE.search(strip_quoted(body))
    if match is None:
        return None
    return RiskFlagData(
        code="sensitive_content",
        severity="error",
        message=f"Обнаружено чувствительное содержание: «{match.group(0)}»",
    )


def detect_external_domain(recipients: list[str], known: set[str]) -> RiskFlagData | None:
    for addr in recipients:
        domain = _domain(addr)
        # An empty `known` means nothing is configured yet — we cannot confirm a
        # domain is internal, so say so rather than stay silent.
        if domain and domain not in known:
            return RiskFlagData(
                code="external_domain",
                severity="warning",
                message=f"Внешний домен получателя: {domain}",
            )
    return None


def detect_language_mismatch(body: str, recipients: list[str]) -> RiskFlagData | None:
    has_cyrillic = any("а" <= ch.lower() <= "я" or ch == "ё" for ch in strip_quoted(body)[:400])
    if not has_cyrillic:
        return None
    for addr in recipients:
        domain = _domain(addr)
        if domain and not domain.endswith((".ru", ".рф", ".su", ".by", ".kz")):
            return RiskFlagData(
                code="language_mismatch",
                severity="warning",
                message=f"Русский текст отправляется на домен {domain}",
            )
    return None


def detect_first_time_recipient(recipients: list[str], known_correspondents: set[str]) -> RiskFlagData | None:
    unseen = [a for a in recipients if a.strip().lower() not in known_correspondents]
    if not unseen:
        return None
    return RiskFlagData(
        code="first_time_recipient",
        severity="warning",
        message=f"Впервые пишем на этот адрес: {', '.join(unseen[:3])}",
        details={"addresses": unseen[:10]},
    )


def detect_stale_reply(parent_sent_at: datetime | None, days: int = 90) -> RiskFlagData | None:
    if parent_sent_at is None:
        return None
    if parent_sent_at.tzinfo is None:
        parent_sent_at = parent_sent_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parent_sent_at
    if age > timedelta(days=days):
        return RiskFlagData(
            code="stale_reply",
            severity="warning",
            message=f"Ответ на письмо {age.days}-дневной давности",
        )
    return None


def blocking_flags(flags: list[RiskFlagData]) -> list[RiskFlagData]:
    return [f for f in flags if f.blocking]


@dataclass
class DraftRiskInput:
    """Всё, что детекторам нужно из БД, — собранное один раз.

    Существует, чтобы список детекторов был ОДИН на все пути отправки. Раньше
    их было три: эндпоинт агента, человеческая отправка и рукописная копия
    внутри правил фильтрации (``email_rules._rule_send_blocked``), у которой
    был свой список слов и не было проверки похожего домена — то есть
    единственный путь без человека был и единственным путём без защиты от
    подмены домена получателя.
    """

    known_domains: set[str] = field(default_factory=set)
    known_correspondents: set[str] = field(default_factory=set)
    parent_sent_at: datetime | None = None
    supplier_email: str | None = None


def evaluate_draft_data(draft_data: dict, ctx: DraftRiskInput) -> list[RiskFlagData]:
    """Чистое ядро: детекторы поверх уже собранных данных, без обращений к БД."""
    data = draft_data or {}
    recipients = [a for a in (data.get("to_addresses") or []) if a]
    recipients += [a for a in (data.get("cc_addresses") or []) if a]
    body = data.get("body_text") or data.get("body_html") or ""
    attachment_count = len(data.get("attachment_ids") or [])

    flags: list[RiskFlagData | None] = [
        detect_lookalike_domain(recipients, ctx.known_domains),
        detect_sensitive_content(body),
        detect_external_domain(recipients, ctx.known_domains),
        detect_promised_attachment(body, attachment_count),
        detect_amount_without_context(body, data.get("context")),
        detect_language_mismatch(body, recipients),
    ]
    if recipients:
        flags.append(detect_first_time_recipient(recipients, ctx.known_correspondents))
    if ctx.parent_sent_at is not None:
        flags.append(detect_stale_reply(ctx.parent_sent_at))
    if ctx.supplier_email:
        if not any(ctx.supplier_email.lower() in a.lower() for a in recipients):
            flags.append(RiskFlagData(
                code="recipient_mismatch",
                severity="warning",
                message=(
                    f"Получатель не совпадает с email поставщика "
                    f"({ctx.supplier_email})"
                ),
            ))
    return [f for f in flags if f is not None]


def _recipient_addresses(data: dict) -> list[str]:
    return [
        a.strip().lower()
        for a in ((data.get("to_addresses") or []) + (data.get("cc_addresses") or []))
        if a
    ]


async def collect_risk_input(db, draft_data: dict) -> DraftRiskInput:
    """Async-сбор контекста детекторов (FastAPI-путь)."""
    import uuid as _uuid

    from sqlalchemy import func, select

    from app.db.models import EmailMessage, Party
    from app.domain.email_rules import known_domains

    data = draft_data or {}
    ctx = DraftRiskInput(known_domains=await known_domains(db))

    lowered = _recipient_addresses(data)
    if lowered:
        seen = (
            await db.execute(
                select(func.lower(EmailMessage.from_address)).where(
                    func.lower(EmailMessage.from_address).in_(lowered)
                )
            )
        ).scalars().all()
        parties = (
            await db.execute(
                select(func.lower(Party.contact_email)).where(
                    func.lower(Party.contact_email).in_(lowered)
                )
            )
        ).scalars().all()
        ctx.known_correspondents = {a for a in [*seen, *parties] if a}

    raw_parent = data.get("in_reply_to_message_id")
    if raw_parent:
        try:
            parent = await db.get(EmailMessage, _uuid.UUID(str(raw_parent)))
        except (ValueError, TypeError):
            parent = None
        if parent is not None:
            ctx.parent_sent_at = parent.sent_at or parent.received_at

    supplier_id = data.get("supplier_id")
    if supplier_id:
        try:
            party = await db.get(Party, _uuid.UUID(str(supplier_id)))
        except (ValueError, TypeError):
            party = None
        if party is not None:
            ctx.supplier_email = party.contact_email

    return ctx


def collect_risk_input_sync(db, draft_data: dict) -> DraftRiskInput:
    """Тот же сбор в синхронной сессии — путь правил фильтрации (ingest)."""
    import uuid as _uuid

    from sqlalchemy import func, select

    from app.db.models import EmailMessage, Party
    from app.domain.email_rules import known_domains_sync

    data = draft_data or {}
    ctx = DraftRiskInput(known_domains=known_domains_sync(db))

    lowered = _recipient_addresses(data)
    if lowered:
        seen = db.execute(
            select(func.lower(EmailMessage.from_address)).where(
                func.lower(EmailMessage.from_address).in_(lowered)
            )
        ).scalars().all()
        parties = db.execute(
            select(func.lower(Party.contact_email)).where(
                func.lower(Party.contact_email).in_(lowered)
            )
        ).scalars().all()
        ctx.known_correspondents = {a for a in [*seen, *parties] if a}

    raw_parent = data.get("in_reply_to_message_id")
    if raw_parent:
        try:
            parent = db.get(EmailMessage, _uuid.UUID(str(raw_parent)))
        except (ValueError, TypeError):
            parent = None
        if parent is not None:
            ctx.parent_sent_at = parent.sent_at or parent.received_at

    supplier_id = data.get("supplier_id")
    if supplier_id:
        try:
            party = db.get(Party, _uuid.UUID(str(supplier_id)))
        except (ValueError, TypeError):
            party = None
        if party is not None:
            ctx.supplier_email = party.contact_email

    return ctx


async def evaluate_draft(db, draft_data: dict) -> list[RiskFlagData]:
    """Every applicable detector for one outbound draft.

    Shared by the agent's ``email.risk_check`` and the human's ``email.send``
    so the two cannot drift apart again.
    """
    return evaluate_draft_data(draft_data, await collect_risk_input(db, draft_data))


def evaluate_draft_sync(db, draft_data: dict) -> list[RiskFlagData]:
    """Синхронный близнец :func:`evaluate_draft` для правил фильтрации."""
    return evaluate_draft_data(draft_data, collect_risk_input_sync(db, draft_data))
