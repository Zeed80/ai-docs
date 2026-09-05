"""IMAP client for multi-mailbox email fetching.

Supports multiple mailboxes (procurement, accounting, general),
each with separate credentials and routing rules.
"""

import email
import hashlib
import imaplib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header
from email.utils import parsedate_to_datetime

import structlog

from app.config import settings

logger = structlog.get_logger()


@dataclass
class MailboxConfig:
    name: str
    host: str
    port: int
    user: str
    password: str
    ssl: bool = True
    folder: str = "INBOX"
    # Routing: what doc types / roles this mailbox serves
    default_doc_type: str | None = None
    assigned_role: str | None = None
    # Personal mailboxes belong to one employee: we must not touch their \Seen
    # flags (the owner reads this mailbox in a normal mail client), so they are
    # polled with BODY.PEEK from a stored UID watermark instead of IMAP UNSEEN.
    mailbox_type: str = "shared"
    owner_sub: str | None = None
    last_seen_uid: int | None = None
    # Ф2 — когда опрашивается не основная папка, водяной знак UID берётся и
    # сохраняется на СТРОКЕ ПАПКИ: UID-ы нумеруются внутри папки, и общий на
    # ящик водяной знак, поднятый подпапкой, заставил бы пропустить письма в
    # INBOX. None = основная папка, водяной знак на ящике (как было).
    watermark_folder: str | None = None
    # "password" (config.password is usable as-is) or "oauth2" — Gmail/Microsoft
    # 365 mailboxes connected via app/api/oauth.py have no usable password at
    # all, fetch_unseen_from_mailbox() re-derives an access token instead.
    auth_method: str = "password"

    @property
    def is_personal(self) -> bool:
        return self.mailbox_type == "personal"


@dataclass
class ParsedAttachment:
    filename: str
    content: bytes
    content_type: str
    size: int
    sha256: str
    # Parts referenced from the HTML body by cid: (logos in signatures, inline
    # screenshots). They are not "attachments" the user chose to send, and
    # listing them as such buries the real ones.
    content_id: str | None = None
    is_inline: bool = False


@dataclass
class ParsedEmail:
    message_id: str | None
    in_reply_to: str | None
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    body_text: str
    body_html: str
    sent_at: datetime | None
    has_attachments: bool
    attachments: list[ParsedAttachment] = field(default_factory=list)
    # body_text was rendered from body_html (no text/plain part in the message)
    body_text_derived: bool = False
    # Full References chain — without it our replies carry a one-element chain
    # and fall out of the thread in the recipient's client.
    references: str | None = None
    # Where replies belong when it is not the From address.
    reply_to: str | None = None
    # Auto-Submitted / Precedence / List-Id / List-Unsubscribe + SPF/DKIM/DMARC.
    headers_meta: dict = field(default_factory=dict)
    # Ф2.1 — the server-side address of this message. Without it nothing we do
    # here can ever be told to the server.
    imap_uid: int | None = None
    imap_folder: str | None = None


def get_mailbox_configs() -> list[MailboxConfig]:
    """Load active mailbox configs from the database."""
    from sqlalchemy import select

    from app.db.models import MailboxConfig as MailboxConfigDB
    from app.db.sync_session import sync_session
    from app.utils.crypto import decrypt_password

    try:
        with sync_session() as db:
            rows = (
                db.execute(
                    select(MailboxConfigDB).where(MailboxConfigDB.is_active == True)  # noqa: E712
                )
                .scalars()
                .all()
            )
            configs = [
                MailboxConfig(
                    name=row.name,
                    host=row.imap_host,
                    port=row.imap_port,
                    user=row.imap_user,
                    password=decrypt_password(row.imap_password_encrypted),
                    ssl=row.imap_ssl,
                    folder=row.imap_folder,
                    default_doc_type=row.default_doc_type,
                    assigned_role=row.assigned_role,
                    mailbox_type=row.mailbox_type or "shared",
                    owner_sub=row.owner_sub,
                    last_seen_uid=row.last_seen_uid,
                    auth_method=row.auth_method or "password",
                )
                for row in rows
            ]
        return configs
    except Exception as e:
        # A DB failure here is a real outage — returning [] pretended "no
        # mailboxes are configured" and turned it into a silently successful,
        # no-op poll. Let the task fail and retry instead.
        logger.error("mailbox_configs_load_failed", error=str(e))
        raise


def decode_mime_header(value: str | None) -> str:
    """Decode MIME encoded header value."""
    if not value:
        return ""
    decoded_parts = decode_header(value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return " ".join(result)


_AUTH_RESULT = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)


def parse_auth_results(msg) -> dict:
    """SPF/DKIM/DMARC verdicts as the receiving server recorded them.

    The agent creates invoices and identifies suppliers from these letters; it
    has to be able to tell a message that failed authentication from one that
    passed. Absent headers mean "unknown", never "pass".
    """
    verdicts: dict[str, str] = {}
    raw = " ".join(
        str(v)
        for k, v in msg.items()
        if k.lower() in ("authentication-results", "received-spf", "arc-authentication-results")
    )
    for name, value in _AUTH_RESULT.findall(raw):
        verdicts.setdefault(name.lower(), value.lower())
    if "spf" not in verdicts and msg.get("Received-SPF"):
        first = str(msg.get("Received-SPF")).strip().split()[0].lower()
        if first:
            verdicts["spf"] = first
    return verdicts


def collect_headers_meta(msg) -> dict:
    """Headers kept for loop protection, unsubscribe and provenance."""
    meta: dict = {}
    for header, key in (
        ("Auto-Submitted", "auto_submitted"),
        ("Precedence", "precedence"),
        ("List-Id", "list_id"),
        ("List-Unsubscribe", "list_unsubscribe"),
        ("Return-Path", "return_path"),
    ):
        value = msg.get(header)
        if value:
            meta[key] = decode_mime_header(str(value))[:500]
    auth = parse_auth_results(msg)
    if auth:
        meta["auth"] = auth
    return meta


def is_automated_message(headers_meta: dict | None) -> bool:
    """True when the sender declared this as machine-generated or bulk.

    The standard signals live in headers; the previous loop guard searched the
    BODY for the word "noreply", which for an HTML-only letter meant searching
    an empty string.
    """
    meta = headers_meta or {}
    auto = str(meta.get("auto_submitted") or "").strip().lower()
    if auto and auto != "no":
        return True
    precedence = str(meta.get("precedence") or "").strip().lower()
    if precedence in ("bulk", "list", "junk", "auto_reply"):
        return True
    return bool(meta.get("list_id"))


def parse_email_message(raw_bytes: bytes) -> ParsedEmail:
    """Parse raw email bytes into structured data."""
    msg = email.message_from_bytes(raw_bytes)

    # Headers
    message_id = msg.get("Message-ID")
    in_reply_to = msg.get("In-Reply-To")
    from_addr = decode_mime_header(msg.get("From", ""))
    to_addrs = [a.strip() for a in decode_mime_header(msg.get("To", "")).split(",") if a.strip()]
    cc_addrs = [a.strip() for a in decode_mime_header(msg.get("Cc", "")).split(",") if a.strip()]
    subject = decode_mime_header(msg.get("Subject", ""))

    references = decode_mime_header(msg.get("References", "")).strip() or None
    reply_to = decode_mime_header(msg.get("Reply-To", "")).strip() or None
    headers_meta = collect_headers_meta(msg)

    # Date. A missing or unparsable header used to leave sent_at NULL, and the
    # whole batch then sorted by received_at — an imported history collapsed
    # into one moment in time.
    sent_at = None
    date_str = msg.get("Date")
    if date_str:
        try:
            sent_at = parsedate_to_datetime(date_str)
        except Exception:
            pass
    if sent_at is None:
        sent_at = datetime.now(UTC)

    # Body
    body_text = ""
    body_html = ""
    attachments: list[ParsedAttachment] = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            content_id = (part.get("Content-ID") or "").strip().strip("<>")
            # Provisional; the authoritative test is whether the HTML body
            # actually references this cid (applied after the walk) — clients
            # disagree about the disposition of embedded images, and some send
            # a referenced logo as Content-Disposition: attachment.
            is_inline = bool(content_id) and "attachment" not in disposition

            if "attachment" in disposition or part.get_filename() or content_id:
                payload = part.get_payload(decode=True)
                if payload:
                    filename = decode_mime_header(part.get_filename()) or (
                        f"inline-{content_id}" if content_id else "attachment"
                    )
                    sha256 = hashlib.sha256(payload).hexdigest()
                    attachments.append(
                        ParsedAttachment(
                            filename=filename,
                            content=payload,
                            content_type=content_type,
                            size=len(payload),
                            sha256=sha256,
                            content_id=content_id or None,
                            is_inline=is_inline,
                        )
                    )
            elif content_type == "text/plain" and not body_text:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_text = payload.decode(charset, errors="replace")
            elif content_type == "text/html" and not body_html:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    body_html = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            if msg.get_content_type() == "text/html":
                body_html = payload.decode(charset, errors="replace")
            else:
                body_text = payload.decode(charset, errors="replace")

    # A part is inline when the body actually points at it. Trusting only the
    # disposition marks referenced logos as attachments (burying the real ones)
    # and unreferenced files as inline (hiding them from the user entirely).
    if attachments:
        html_lower = (body_html or "").lower()
        for att in attachments:
            if att.content_id:
                att.is_inline = f"cid:{att.content_id.lower()}" in html_lower

    # HTML-only mail is the common case in business correspondence, and an
    # empty body_text propagates far: FTS never finds the letter, filter rules
    # matching on `body` never fire, the auto-reply loop guard has nothing to
    # look at, the thread list shows a blank preview, and the agent is handed a
    # message it reports as empty. Render the HTML instead of storing nothing.
    body_text_derived = False
    if not body_text.strip() and body_html.strip():
        from app.domain.email_html import html_to_text

        derived = html_to_text(body_html)
        if derived:
            body_text = derived
            body_text_derived = True

    return ParsedEmail(
        message_id=message_id,
        in_reply_to=in_reply_to,
        from_address=from_addr,
        to_addresses=to_addrs,
        cc_addresses=cc_addrs,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        sent_at=sent_at,
        # Only real attachments count: a signature logo must not make the
        # thread list show a paperclip.
        has_attachments=any(not a.is_inline for a in attachments),
        attachments=attachments,
        body_text_derived=body_text_derived,
        references=references,
        reply_to=reply_to,
        headers_meta=headers_meta,
    )


def _message_too_big(conn, msg_id, max_bytes: int, *, uid: bool) -> bool:
    """RFC822.SIZE check before pulling the body.

    Without it a single 60 MB message is read whole into the worker's memory.
    A size we cannot determine is treated as acceptable — refusing to fetch on
    a parse failure would silently drop ordinary mail.
    """
    import re as _re

    try:
        if uid:
            status, data = conn.uid("fetch", msg_id, "(RFC822.SIZE)")
        else:
            status, data = conn.fetch(msg_id, "(RFC822.SIZE)")
        if status != "OK" or not data or not data[0]:
            return False
        raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
        match = _re.search(rb"RFC822\.SIZE\s+(\d+)", raw)
        return bool(match) and int(match.group(1)) > max_bytes
    except Exception:  # noqa: BLE001
        return False


def _known_max_uid(mailbox_name: str, remote_folder: str) -> int:
    """Наибольший UID письма этой папки, которое у нас уже есть.

    Нужно ровно один раз — при переходе общего ящика с поиска по ``UNSEEN`` на
    водяной знак UID. Без этого знак стартовал бы с нуля и ящик переингестился
    бы целиком: защита по message_id_header спасла бы от дублей в базе, но
    правила автоответа успели бы отработать на всей истории.
    """
    from sqlalchemy import func as _f
    from sqlalchemy import select as sa_select

    from app.db.models import EmailMessage
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            return int(
                db.execute(
                    sa_select(_f.max(EmailMessage.imap_uid)).where(
                        EmailMessage.mailbox == mailbox_name,
                        EmailMessage.imap_folder == remote_folder,
                    )
                ).scalar()
                or 0
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "imap_known_max_uid_failed",
            mailbox=mailbox_name,
            folder=remote_folder,
            error=str(exc),
        )
        return 0


def _folder_state(mailbox_name: str, remote_folder: str) -> tuple[int, int | None]:
    """(водяной знак UID, сохранённый UIDVALIDITY) для папки."""
    from sqlalchemy import select as sa_select

    from app.db.models import MailboxFolder
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            row = db.execute(
                sa_select(MailboxFolder.last_seen_uid, MailboxFolder.uid_validity).where(
                    MailboxFolder.mailbox == mailbox_name,
                    MailboxFolder.remote_name == remote_folder,
                )
            ).first()
            return (int(row[0] or 0), row[1]) if row else (0, None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "imap_folder_state_failed",
            mailbox=mailbox_name,
            folder=remote_folder,
            error=str(exc),
        )
        return 0, None


def _save_folder_uid_validity(mailbox_name: str, remote_folder: str, validity: int) -> None:
    from sqlalchemy import select as sa_select

    from app.db.models import MailboxFolder
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            row = db.execute(
                sa_select(MailboxFolder).where(
                    MailboxFolder.mailbox == mailbox_name,
                    MailboxFolder.remote_name == remote_folder,
                )
            ).scalar_one_or_none()
            if row is not None and row.uid_validity != validity:
                row.uid_validity = validity
                row.last_seen_uid = 0
                db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "imap_uid_validity_save_failed",
            mailbox=mailbox_name,
            folder=remote_folder,
            error=str(exc),
        )


def _save_folder_last_seen_uid(mailbox_name: str, remote_name: str, uid: int) -> None:
    """Ф2 — водяной знак подпапки. См. MailboxConfig.watermark_folder."""
    from sqlalchemy import select as sa_select

    from app.db.models import MailboxFolder
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            row = db.execute(
                sa_select(MailboxFolder).where(
                    MailboxFolder.mailbox == mailbox_name,
                    MailboxFolder.remote_name == remote_name,
                )
            ).scalar_one_or_none()
            if row is None:
                # Папка ещё не открыта discover_folders. Для основной папки
                # знак дублируется на строке ящика, для остальных прогресс
                # просто не сохранится — молчать об этом нельзя.
                logger.warning(
                    "imap_folder_row_missing_for_watermark",
                    mailbox=mailbox_name,
                    folder=remote_name,
                )
                return
            if (row.last_seen_uid or 0) < uid:
                row.last_seen_uid = uid
                db.commit()
    except Exception as exc:  # noqa: BLE001
        # Losing the watermark re-delivers messages; the message_id_header
        # unique index makes that a no-op, so it must not fail the poll.
        logger.warning(
            "imap_folder_watermark_save_failed",
            mailbox=mailbox_name,
            folder=remote_name,
            error=str(exc),
        )


def folder_last_seen_uid(mailbox_name: str, remote_name: str) -> int:
    from sqlalchemy import select as sa_select

    from app.db.models import MailboxFolder
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            return int(
                db.execute(
                    sa_select(MailboxFolder.last_seen_uid).where(
                        MailboxFolder.mailbox == mailbox_name,
                        MailboxFolder.remote_name == remote_name,
                    )
                ).scalar_one_or_none()
                or 0
            )
    except Exception:  # noqa: BLE001
        return 0


def _save_last_seen_uid(mailbox_name: str, uid: int) -> None:
    """Persist the UID watermark for a PEEK-polled (personal) mailbox."""
    from sqlalchemy import select

    from app.db.models import MailboxConfig as MailboxConfigDB
    from app.db.sync_session import sync_session

    try:
        with sync_session() as db:
            row = db.execute(
                select(MailboxConfigDB).where(MailboxConfigDB.name == mailbox_name)
            ).scalar_one_or_none()
            if row is not None and (row.last_seen_uid or 0) < uid:
                row.last_seen_uid = uid
                db.commit()
    except Exception as e:  # noqa: BLE001
        # Losing the watermark only means re-reading messages next run (dedup
        # happens downstream by Message-ID/hash) — never fail the fetch for it.
        logger.warning("imap_uid_watermark_save_failed", mailbox=mailbox_name, error=str(e))


def _quoted_folder(name: str) -> str:
    """IMAP-имя папки для SELECT.

    Найдено на живом стенде: у mail.ru есть папка ``INBOX/Public services``, и
    без кавычек пробел делает из неё два аргумента — сервер отвечает
    ``BAD [CLIENTBUG] Invalid number of arguments``. Уже закавыченное имя не
    закавычиваем повторно.
    """
    if name.startswith('"') and name.endswith('"'):
        return name
    if any(ch in name for ch in ' ()"{%*]\\'):
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return name


def fetch_unseen_from_mailbox(config: MailboxConfig) -> list[ParsedEmail]:
    """Fetch new messages from a mailbox.

    Новое ищется по водяному знаку UID для ЛЮБОГО типа ящика: UID > last_seen_uid.
    Общий ящик раньше опрашивался поиском UNSEEN исходя из того, что в
    интеграционный ящик руками никто не заходит. Допущение ломается двумя
    способами, и оба воспроизведены на живом сервере: провайдер сам помечает
    прочитанным письмо самому себе, а письмо, открытое человеком в другом
    клиенте раньше нашего опроса, не попадает к нам никогда — без ошибки,
    молча.

    Разница между типами осталась только в флагах: личный ящик читает человек,
    поэтому \\Seen не трогаем (BODY.PEEK); общему письмо помечаем прочитанным —
    это его состояние обработки, но уже не критерий отбора.
    """
    logger.info(
        "imap_connecting",
        mailbox=config.name,
        host=config.host,
        mode="peek" if config.is_personal else "seen-marking",
    )

    try:
        # A timeout is not optional here: imaplib defaults to blocking forever,
        # so one unresponsive server permanently consumed an ingest worker and
        # the task never failed, never retried and never reported anything.
        timeout = settings.imap_timeout_seconds
        if config.ssl:
            conn = imaplib.IMAP4_SSL(config.host, config.port, timeout=timeout)
        else:
            conn = imaplib.IMAP4(config.host, config.port, timeout=timeout)

        if config.auth_method == "oauth2":
            from sqlalchemy import select as sa_select

            from app.db.models import MailboxConfig as MailboxConfigDB
            from app.db.sync_session import sync_session
            from app.domain.oauth_mail import get_valid_access_token_sync, imap_xoauth2_authobject

            with sync_session() as oauth_db:
                row = oauth_db.execute(
                    sa_select(MailboxConfigDB).where(MailboxConfigDB.name == config.name)
                ).scalar_one_or_none()
                if not row:
                    raise RuntimeError(f"Mailbox '{config.name}' disappeared before OAuth login")
                access_token = get_valid_access_token_sync(oauth_db, row)
            conn.authenticate("XOAUTH2", imap_xoauth2_authobject(config.user, access_token))
        else:
            conn.login(config.user, config.password)
        select_status, select_data = conn.select(_quoted_folder(config.folder))

        personal = config.is_personal

        # Новое ищем по водяному знаку UID, а НЕ по флагу \Seen — для любого
        # типа ящика. Общий ящик раньше опрашивался поиском UNSEEN исходя из
        # того, что «в интеграционный ящик руками никто не заходит». Допущение
        # ломается сразу двумя способами, и оба проверены на живом сервере:
        # mail.ru сам помечает прочитанным письмо самому себе, а человек,
        # открывший письмо в другом клиенте раньше нашего опроса, навсегда
        # лишает нас этого письма — молча, без единой ошибки. Флаг \Seen для
        # общего ящика по-прежнему ставим: это его состояние обработки, просто
        # больше не критерий отбора.
        from app.domain.imap_sync import parse_select_response

        meta = parse_select_response(select_data, getattr(conn, "untagged_responses", None))
        remote_folder = config.folder
        stored_uid, stored_validity = _folder_state(config.name, remote_folder)
        validity = meta.get("uid_validity")
        if validity and stored_validity and validity != stored_validity:
            # Сервер пересоздал папку: прежние UID указывают на другие письма,
            # продолжать с них нельзя — папка переиндексируется целиком.
            logger.warning(
                "imap_uid_validity_changed",
                mailbox=config.name,
                folder=remote_folder,
                was=stored_validity,
                now=validity,
            )
            stored_uid = 0
        if validity and validity != stored_validity:
            _save_folder_uid_validity(config.name, remote_folder, validity)

        watermark = stored_uid
        if not watermark and config.watermark_folder is None:
            watermark = int(config.last_seen_uid or 0)
        if not watermark:
            # Первый проход после перехода с UNSEEN: начинаем не с нуля, а с
            # того, что у нас уже есть, иначе ящик переингестится целиком.
            watermark = _known_max_uid(config.name, remote_folder)
            if watermark:
                logger.info(
                    "imap_watermark_seeded_from_history",
                    mailbox=config.name,
                    folder=remote_folder,
                    uid=watermark,
                )

        status, message_ids = conn.uid("search", None, f"UID {watermark + 1}:*")

        if status != "OK" or not message_ids or not message_ids[0]:
            logger.info("imap_no_new_messages", mailbox=config.name)
            conn.logout()
            return []

        ids = message_ids[0].split()
        # Cap the batch: the first sync of a years-old mailbox otherwise pulls
        # everything into one task. The remainder is picked up next tick — the
        # UID watermark and \Seen flags make that safe to resume.
        max_per_poll = max(1, settings.imap_max_messages_per_poll)
        truncated = len(ids) > max_per_poll
        if truncated:
            # Самые старые из найденных: водяной знак двигается вперёд, и
            # остаток заберёт следующий тик.
            ids = ids[:max_per_poll]
        logger.info(
            "imap_found_messages",
            mailbox=config.name,
            count=len(ids),
            truncated=truncated,
        )

        max_bytes = max(1, settings.imap_max_message_mb) * 1024 * 1024

        emails: list[ParsedEmail] = []
        max_uid = watermark
        # Поиск идёт по UID, поэтому отдельное сопоставление «порядковый номер
        # → UID» больше не нужно: ids и есть UID, которыми адресуется любая
        # последующая операция write-back.

        for msg_id in ids:
            # "UID n:*" всегда возвращает хотя бы одно письмо, даже когда n уже
            # за концом папки — всё, что не выше знака, пропускаем.
            try:
                uid_value = int(msg_id)
            except (TypeError, ValueError):
                continue
            if uid_value <= watermark:
                continue

            if _message_too_big(conn, msg_id, max_bytes, uid=True):
                logger.warning(
                    "imap_message_oversized",
                    mailbox=config.name,
                    uid=uid_value,
                )
                max_uid = max(max_uid, uid_value)
                continue

            # Личный ящик человек читает сам — \Seen не трогаем (PEEK).
            # Общему письмо помечаем прочитанным: это его состояние обработки.
            status, data = conn.uid("fetch", msg_id, "(BODY.PEEK[])")

            if status != "OK" or not data or not data[0]:
                continue

            raw = data[0][1]
            if isinstance(raw, bytes):
                parsed = parse_email_message(raw)
                parsed.imap_folder = config.folder
                parsed.imap_uid = uid_value
                emails.append(parsed)
                max_uid = max(max_uid, uid_value)
                if not personal:
                    try:
                        conn.uid("store", msg_id, "+FLAGS", "\\Seen")
                    except Exception as exc:  # noqa: BLE001
                        # Флаг больше не критерий отбора, поэтому его потеря
                        # ничего не ломает — но молчать о ней не нужно.
                        logger.warning(
                            "imap_seen_flag_failed",
                            mailbox=config.name,
                            uid=uid_value,
                            error=str(exc),
                        )

        conn.logout()

        if max_uid > watermark:
            _save_folder_last_seen_uid(config.name, remote_folder, max_uid)
            if config.watermark_folder is None:
                # Совместимость: основная папка продолжает вести знак и на
                # строке ящика — его читают старые пути.
                _save_last_seen_uid(config.name, max_uid)

        logger.info("imap_fetched", mailbox=config.name, count=len(emails))
        return emails

    except Exception as e:
        # Do NOT swallow into []: a login failure, an expired OAuth token or an
        # unreachable host must surface as sync_error on the mailbox, not look
        # like "the inbox is empty". An empty result is a legitimate return
        # above (imap_no_new_messages / imap_error-free); an exception is not.
        logger.error("imap_error", mailbox=config.name, error=str(e))
        try:
            conn.logout()
        except Exception:
            pass
        raise
