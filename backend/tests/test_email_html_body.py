"""Ф1.1 — HTML-only mail must not arrive with an empty body.

Most business correspondence has no text/plain part. ``body_text`` stayed empty
for those messages, and every consumer reads that column: the Russian FTS index
(so the letter could not be found), filter rules matching on ``body``, the
auto-reply loop guard, the thread-list snippet, and what the agent is handed
when asked to read a letter — it reported the message as empty.
"""

import uuid
from datetime import UTC, datetime
from email.message import EmailMessage as MimeMessage

from app.domain.email_html import html_to_text
from app.tasks.imap_client import parse_email_message

HTML_BODY = """
<html><head><style>p{color:red}</style></head><body>
<p>Добрый день!</p>
<p>Во вложении <b>счёт</b> №УТ-2562 на сумму 240&nbsp;000 руб.</p>
<ul><li>Труба 20х2</li><li>Фланец плоский</li></ul>
<table><tr><td>Итого</td><td>240000</td></tr></table>
<script>alert(1)</script>
</body></html>
"""


def _mime(*, html: str | None = None, text: str | None = None) -> bytes:
    msg = MimeMessage()
    msg["Subject"] = "Счёт УТ-2562"
    msg["From"] = "Поставщик <supplier@example.com>"
    msg["To"] = "procurement@example.com"
    msg["Message-ID"] = f"<{uuid.uuid4()}@example.com>"
    msg["Date"] = "Tue, 26 Aug 2026 10:00:00 +0300"
    if text is not None and html is not None:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(text or "")
    return msg.as_bytes()


def test_html_only_message_gets_readable_text():
    parsed = parse_email_message(_mime(html=HTML_BODY))

    assert parsed.body_text_derived is True
    assert "Добрый день!" in parsed.body_text
    assert "УТ-2562" in parsed.body_text
    assert "Труба 20х2" in parsed.body_text
    # Structure survives: a price table must not read as "Итого240000"
    assert "Итого 240000" in parsed.body_text
    # …and script/style never leak into the text
    assert "alert(1)" not in parsed.body_text
    assert "color:red" not in parsed.body_text


def test_real_text_part_is_never_overwritten():
    parsed = parse_email_message(_mime(text="Настоящий текст письма", html=HTML_BODY))

    assert parsed.body_text.strip() == "Настоящий текст письма"
    assert parsed.body_text_derived is False


def test_plain_text_only_is_untouched():
    parsed = parse_email_message(_mime(text="Просто текст"))
    assert parsed.body_text.strip() == "Просто текст"
    assert parsed.body_text_derived is False


def test_html_to_text_handles_broken_markup_and_entities():
    assert html_to_text("<p>сломанный <b>html") == "сломанный html"
    assert html_to_text("A &amp; B &lt;тест&gt;") == "A & B <тест>"
    assert html_to_text("") == ""
    assert html_to_text(None) == ""


def test_line_breaks_are_not_doubled():
    assert html_to_text("<div>Строка 1<br>Строка 2</div>") == "Строка 1\nСтрока 2"


async def test_html_only_message_is_findable_by_search(client, db_session):
    """The end this fix exists for: the letter must be searchable."""
    from app.db.models import EmailMessage, EmailThread, MailboxConfig

    db_session.add(
        MailboxConfig(
            name="procurement",
            display_name="Закупки",
            imap_host="mail.example.com",
            imap_port=993,
            imap_user="procurement",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
        )
    )
    parsed = parse_email_message(_mime(html=HTML_BODY))
    thread = EmailThread(subject=parsed.subject, mailbox="procurement", message_count=1)
    db_session.add(
        EmailMessage(
            thread=thread,
            mailbox="procurement",
            subject=parsed.subject,
            from_address=parsed.from_address,
            to_addresses=parsed.to_addresses,
            body_text=parsed.body_text,
            body_html=parsed.body_html,
            body_text_derived=parsed.body_text_derived,
            received_at=datetime.now(UTC),
            message_id_header=parsed.message_id,
        )
    )
    await db_session.commit()

    resp = await client.post("/api/email/search", json={"query": "Труба", "limit": 20})
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["body_text_derived"] is True


# ── Ф1.2: headers the parser used to throw away ────────────────────────────


def _mime_with_headers(**headers: str) -> bytes:
    msg = MimeMessage()
    msg["Subject"] = "Re: Счёт"
    msg["From"] = "no-reply@supplier.example"
    msg["To"] = "procurement@example.com"
    msg["Message-ID"] = f"<{uuid.uuid4()}@supplier.example>"
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    msg.set_content("текст")
    return msg.as_bytes()


def test_references_and_reply_to_are_kept():
    raw = _mime_with_headers(
        References="<a@x> <b@x>",
        In_Reply_To="<b@x>",
        Reply_To="Отдел продаж <sales@supplier.example>",
        Date="Tue, 26 Aug 2026 10:00:00 +0300",
    )
    parsed = parse_email_message(raw)

    # References was a column that nothing ever wrote, so every reply we sent
    # carried a one-element chain and fell out of the recipient's thread.
    assert parsed.references == "<a@x> <b@x>"
    assert "sales@supplier.example" in parsed.reply_to


def test_reply_threading_uses_the_full_chain():
    from app.db.models import EmailMessage
    from app.domain.email_thread import resolve_threading_headers

    parent = EmailMessage(
        mailbox="procurement",
        from_address="x@y.z",
        subject="s",
        message_id_header="<b@x>",
        references="<a@x>",
    )
    in_reply_to, references = resolve_threading_headers(parent)
    assert in_reply_to == "<b@x>"
    assert references == "<a@x> <b@x>"


def test_automated_and_bulk_mail_is_recognised_by_headers():
    from app.tasks.imap_client import is_automated_message

    assert is_automated_message(
        parse_email_message(_mime_with_headers(Auto_Submitted="auto-replied")).headers_meta
    )
    assert is_automated_message(
        parse_email_message(_mime_with_headers(Precedence="bulk")).headers_meta
    )
    assert is_automated_message(
        parse_email_message(_mime_with_headers(List_Id="<news.example.com>")).headers_meta
    )
    # A normal letter is not automated, and neither is Auto-Submitted: no
    assert not is_automated_message(parse_email_message(_mime_with_headers()).headers_meta)
    assert not is_automated_message(
        parse_email_message(_mime_with_headers(Auto_Submitted="no")).headers_meta
    )


def test_authentication_verdicts_are_extracted():
    parsed = parse_email_message(
        _mime_with_headers(
            Authentication_Results="mx.example.com; spf=fail smtp.mailfrom=evil.example; dkim=none; dmarc=fail",
        )
    )
    auth = parsed.headers_meta["auth"]
    assert auth["spf"] == "fail"
    assert auth["dkim"] == "none"
    assert auth["dmarc"] == "fail"

    # Absent headers mean "unknown", never an implied pass.
    assert "auth" not in parse_email_message(_mime_with_headers()).headers_meta


def test_missing_date_falls_back_instead_of_null():
    parsed = parse_email_message(_mime_with_headers(Date="не дата"))
    assert parsed.sent_at is not None


# ── Ф1.3: inline images referenced by cid: ─────────────────────────────────


def _mime_with_inline_logo() -> bytes:
    msg = MimeMessage()
    msg["Subject"] = "Письмо с подписью"
    msg["From"] = "supplier@example.com"
    msg["To"] = "procurement@example.com"
    msg["Message-ID"] = f"<{uuid.uuid4()}@example.com>"
    msg["Date"] = "Tue, 26 Aug 2026 10:00:00 +0300"
    msg.set_content("текст")
    msg.add_alternative(
        '<html><body><p>Добрый день</p><img src="cid:logo@firm"></body></html>',
        subtype="html",
    )
    msg.get_payload()[1].add_related(
        b"\x89PNG fake",
        maintype="image",
        subtype="png",
        cid="<logo@firm>",
        filename="logo.png",
    )
    msg.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="Счёт.pdf")
    return msg.as_bytes()


def test_inline_logo_is_separated_from_real_attachments():
    parsed = parse_email_message(_mime_with_inline_logo())

    inline = [a for a in parsed.attachments if a.is_inline]
    real = [a for a in parsed.attachments if not a.is_inline]
    assert len(inline) == 1 and inline[0].content_id == "logo@firm"
    assert [a.filename for a in real] == ["Счёт.pdf"]
    # A signature logo must not make the thread list show a paperclip.
    assert parsed.has_attachments is True  # because of the real PDF


def test_message_with_only_a_logo_has_no_attachments():
    msg = MimeMessage()
    msg["Subject"] = "Только подпись"
    msg["From"] = "x@y.z"
    msg["To"] = "procurement@example.com"
    msg["Message-ID"] = f"<{uuid.uuid4()}@y.z>"
    msg.set_content("текст")
    msg.add_alternative('<html><body><img src="cid:sig"></body></html>', subtype="html")
    msg.get_payload()[1].add_related(b"\x89PNG", maintype="image", subtype="png", cid="<sig>")
    parsed = parse_email_message(msg.as_bytes())

    assert parsed.has_attachments is False
    assert all(a.is_inline for a in parsed.attachments)


def test_cid_sources_are_rewritten_to_our_endpoint():
    from app.domain.email_html import rewrite_cid_images

    out = rewrite_cid_images('<img src="cid:logo@firm"><img src=cid:second>', "MSG")
    assert "/api/email/messages/MSG/attachments/cid/logo%40firm/content" in out
    assert "/api/email/messages/MSG/attachments/cid/second/content" in out
    assert "cid:" not in out
    # External images are left for the block-remote-images pass (Ф1.4), not touched here
    assert rewrite_cid_images('<img src="https://t/x.gif">', "M") == '<img src="https://t/x.gif">'


def test_inline_parts_are_hidden_from_the_attachment_list():
    from app.domain.email import EmailAttachmentOut, EmailMessageOut

    msg = EmailMessageOut(
        id=uuid.uuid4(),
        thread_id=None,
        message_id_header="<x@y>",
        mailbox="procurement",
        from_address="x@y.z",
        to_addresses=[],
        cc_addresses=[],
        subject="s",
        body_text="t",
        sent_at=None,
        received_at=None,
        has_attachments=True,
        attachment_count=1,
        attachments_meta=[],
        attachments=[
            EmailAttachmentOut(id=uuid.uuid4(), filename="logo.png", is_inline=True),
            EmailAttachmentOut(id=uuid.uuid4(), filename="Счёт.pdf", is_inline=False),
        ],
        is_inbound=True,
        created_at=datetime.now(UTC),
    )
    assert [a.filename for a in msg.attachments] == ["Счёт.pdf"]


# ── Ф1.4: remote images are read receipts ──────────────────────────────────


def test_remote_images_are_blocked_but_recoverable():
    from app.domain.email_html import block_remote_images

    html = (
        "<p>Счёт</p>"
        '<img src="https://track.example/pixel.gif?u=42">'
        '<img src="/api/email/messages/M/attachments/cid/logo/content">'
        '<div style="background-image: url(http://track.example/bg.png)">x</div>'
    )
    out, blocked = block_remote_images(html)

    import re as _re

    assert blocked == 2
    # No live src attribute points at the tracker any more — the URL is parked,
    # not deleted, which is what makes "показать" possible without a refetch.
    live_srcs = _re.findall(r'(?<!data-blocked-)src="([^"]+)"', out)
    assert not any("track.example" in u for u in live_srcs)
    assert 'data-blocked-src="https://track.example/pixel.gif?u=42"' in out
    assert "background-image:none" in out
    # …while our own inline endpoint keeps working.
    assert 'src="/api/email/messages/M/attachments/cid/logo/content"' in out

    # "Показать" is a pure client-side swap of the parked URL.
    shown = out.replace("data-blocked-src=", "src=")
    assert 'src="https://track.example/pixel.gif?u=42"' in shown


def test_blocking_leaves_plain_letters_alone():
    from app.domain.email_html import block_remote_images

    html = "<p>Обычное письмо без картинок</p>"
    out, blocked = block_remote_images(html)
    assert blocked == 0 and out == html


async def test_message_can_be_downloaded_as_eml(client, db_session, monkeypatch):
    """Ф5.3 — honest reconstruction, explicitly not called "оригинал": the raw
    RFC822 bytes are not stored anywhere."""
    import email as _email

    import app.storage as storage
    from app.db.models import EmailAttachment, EmailMessage, EmailThread, MailboxConfig

    monkeypatch.setattr(storage, "download_file", lambda path: b"%PDF-1.4")
    db_session.add(
        MailboxConfig(
            name="procurement",
            display_name="Закупки",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="procurement",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
        )
    )
    thread = EmailThread(subject="Счёт", mailbox="procurement", message_count=1)
    msg = EmailMessage(
        thread=thread,
        mailbox="procurement",
        subject="Счёт УТ-2562",
        from_address="sales@romex.example",
        to_addresses=["procurement@example.com"],
        body_text="во вложении счёт",
        body_html="<p>во вложении счёт</p>",
        references="<a@x> <b@x>",
        reply_to="sales@romex.example",
        received_at=datetime.now(UTC),
        sent_at=datetime.now(UTC),
        message_id_header=f"<{uuid.uuid4()}@romex.example>",
    )
    db_session.add_all([thread, msg])
    await db_session.flush()
    db_session.add(
        EmailAttachment(
            message_id=msg.id,
            filename="Счёт.pdf",
            content_type="application/pdf",
            size=8,
            storage_path="documents/aa/bb/cc",
            sha256="d" * 64,
        )
    )
    await db_session.commit()

    resp = await client.get(f"/api/email/messages/{msg.id}/raw")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("message/rfc822")
    assert resp.headers["content-disposition"].startswith("attachment;")

    parsed = _email.message_from_bytes(resp.content)
    # Headers are MIME-encoded, as they must be in a real .eml.
    from app.tasks.imap_client import decode_mime_header

    assert decode_mime_header(parsed["Subject"]) == "Счёт УТ-2562"
    assert parsed["References"] == "<a@x> <b@x>"
    # Marked as rebuilt, so nobody mistakes it for the delivered bytes.
    assert parsed["X-AI-Docs-Reconstructed"] == "yes"
    names = [p.get_filename() for p in parsed.walk() if p.get_filename()]
    assert "Счёт.pdf" in names
