"""Ф2 — the client and the server must not drift apart.

Before this, state flowed one way: the poller read a single configured folder
and wrote to Postgres, and every action in our UI stopped there. Archiving a
letter here left it unread in Outlook forever, and mail the server filed
anywhere else — mail.ru's own INBOX/ToMyself, as the live stand showed — was
invisible.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.db.models import (
    EmailMessage,
    EmailSyncOp,
    EmailThread,
    MailboxConfig,
    MailboxFolder,
)
from app.domain.imap_sync import (
    decode_mailbox_name,
    encode_mailbox_name,
    parse_flags_response,
    parse_list_response,
    parse_select_response,
)


# ── protocol parsing (no server needed) ────────────────────────────────────


def test_cyrillic_folder_names_round_trip():
    """Modified UTF-7 is not cosmetic: a Cyrillic folder name is what broke the
    first live IMAP check with an ascii codec error."""
    assert decode_mailbox_name("&BCEEPwQwBDw-") == "Спам"
    assert decode_mailbox_name("INBOX/ToMyself") == "INBOX/ToMyself"
    assert decode_mailbox_name("&-") == "&"

    for name in ("Спам", "Отправленные", "INBOX", "INBOX/ToMyself", "A&B"):
        assert decode_mailbox_name(encode_mailbox_name(name)) == name


def test_special_use_and_name_hints_map_to_our_folders():
    folders = parse_list_response([
        rb'(\HasNoChildren \Sent) "|" "Sent"',
        rb'(\HasChildren) "/" "INBOX"',
        rb'(\HasNoChildren) "/" "INBOX/ToMyself"',
        # As the server actually sends it: modified UTF-7, not readable text.
        rb'(\HasNoChildren) "/" "&BBoEPgRABDcEOAQ9BDA-"',
        rb'(\HasNoChildren) "/" "Work"',
        rb'(\Noselect \HasChildren) "/" "[Gmail]"',
    ])
    by_name = {f.name: f for f in folders}

    assert by_name["Sent"].local_folder == "sent"          # via SPECIAL-USE
    assert by_name["INBOX"].local_folder == "inbox"
    trash = by_name["&BBoEPgRABDcEOAQ9BDA-"]
    assert decode_mailbox_name(trash.name) == "Корзина"
    # Recognised through the ENCODED name: matching raw would never fire, and
    # the folder would silently not be our Trash.
    assert trash.local_folder == "trash"
    # A sub-folder of INBOX is where the server files incoming mail on its own
    # (mail.ru's ToMyself, provider auto-sorting) — it belongs in the inbox.
    # Leaving it unmapped meant those letters existed on the server and never
    # appeared here.
    assert by_name["INBOX/ToMyself"].local_folder == "inbox"
    # A top-level folder we cannot name is still not guessed at: assuming
    # "Work" is the inbox would be inventing a mapping nobody asked for.
    assert by_name["Work"].local_folder is None
    assert by_name["[Gmail]"].selectable is False


def test_select_state_comes_from_untagged_responses():
    """imaplib's select() returns only the message count; UIDVALIDITY arrives
    as an untagged response. Reading the return value alone left every folder
    on the live mailbox with no uid_validity at all."""
    meta = parse_select_response(
        [b"3"],
        {"UIDVALIDITY": [b"1787859013"], "UIDNEXT": [b"13"], "HIGHESTMODSEQ": [b"90210"]},
    )
    assert meta == {"uid_validity": 1787859013, "uid_next": 13, "highest_modseq": 90210}

    # Servers that inline it in the response text still work.
    assert parse_select_response(
        [b"* OK [UIDVALIDITY 1699999999] UIDs valid", b"* OK [UIDNEXT 512] next"]
    ) == {"uid_validity": 1699999999, "uid_next": 512}


def test_stored_folder_names_are_used_verbatim():
    """remote_name is kept exactly as LIST returned it — already modified
    UTF-7. Encoding it again turns "&BCEEPwQwBDw-" into "&-BCEEPwQwBDw-" and
    every SELECT of a Cyrillic folder fails, which is what happened live."""
    wire = "&BCEEPwQwBDw-"
    assert decode_mailbox_name(wire) == "Спам"
    assert encode_mailbox_name(wire) != wire      # double-encoding corrupts it
    assert encode_mailbox_name(decode_mailbox_name(wire)) == wire


def test_flag_responses_are_parsed():

    flags = parse_flags_response([
        (rb"1 (UID 17 FLAGS (\Seen \Flagged))", b""),
        (rb"2 (UID 18 FLAGS ())", b""),
    ])
    assert flags[17] == {"\\Seen", "\\Flagged"}
    assert flags[18] == set()


# ── write-back queue ───────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def synced_mailbox(db_session):
    db_session.add(MailboxConfig(
        name="procurement", display_name="Закупки", imap_host="m.example.com",
        imap_port=993, imap_user="procurement", imap_password_encrypted="x",
        imap_ssl=True, is_active=True,
    ))
    db_session.add_all([
        MailboxFolder(mailbox="procurement", remote_name="INBOX",
                      local_folder="inbox", sync_enabled=True),
        MailboxFolder(mailbox="procurement", remote_name="Archive",
                      local_folder="archive", special_use="\\Archive", sync_enabled=True),
    ])
    thread = EmailThread(subject="Счёт", mailbox="procurement", message_count=1,
                         folder="inbox")
    msg = EmailMessage(
        thread=thread, mailbox="procurement", subject="Счёт",
        from_address="x@y.example", to_addresses=["procurement@example.com"],
        received_at=datetime.now(timezone.utc),
        message_id_header=f"<{uuid.uuid4()}@y.example>",
        imap_uid=17, imap_folder="INBOX", is_read=False,
    )
    db_session.add_all([thread, msg])
    await db_session.commit()
    return thread, msg


async def test_marking_read_queues_a_server_op(
    client: AsyncClient, db_session, synced_mailbox, monkeypatch
):
    from sqlalchemy import select

    import app.tasks.email_sync as sync

    monkeypatch.setattr(sync.push_ops, "apply_async", lambda *a, **k: None)
    thread, msg = synced_mailbox

    resp = await client.post("/api/email/threads/actions",
                             json={"thread_ids": [str(thread.id)], "action": "read"})
    assert resp.status_code == 200

    ops = (await db_session.execute(select(EmailSyncOp))).scalars().all()
    assert [o.op for o in ops] == ["seen"]
    assert ops[0].message_id == msg.id
    assert ops[0].state == "pending"


async def test_archiving_queues_a_move_to_the_mapped_server_folder(
    client: AsyncClient, db_session, synced_mailbox, monkeypatch
):
    from sqlalchemy import select

    import app.tasks.email_sync as sync

    monkeypatch.setattr(sync.push_ops, "apply_async", lambda *a, **k: None)
    thread, _ = synced_mailbox

    await client.post("/api/email/threads/actions",
                      json={"thread_ids": [str(thread.id)], "action": "archive"})

    ops = (await db_session.execute(select(EmailSyncOp))).scalars().all()
    move = [o for o in ops if o.op == "move"]
    assert len(move) == 1
    assert move[0].payload["remote_folder"] == "Archive"


async def test_a_move_with_no_mapped_server_folder_is_not_invented(
    client: AsyncClient, db_session, synced_mailbox, monkeypatch
):
    """Better to leave the letter where it is than to guess a destination."""
    from sqlalchemy import select

    import app.tasks.email_sync as sync

    monkeypatch.setattr(sync.push_ops, "apply_async", lambda *a, **k: None)
    thread, _ = synced_mailbox

    await client.post("/api/email/threads/actions",
                      json={"thread_ids": [str(thread.id)], "action": "spam"})

    ops = (await db_session.execute(select(EmailSyncOp))).scalars().all()
    assert [o.op for o in ops] == []          # no Spam folder mapped
    await db_session.refresh(thread)
    assert thread.folder == "spam"            # local state still correct


async def test_a_message_without_a_uid_is_skipped_not_queued(
    client: AsyncClient, db_session, synced_mailbox, monkeypatch
):
    """Our own outbound copy has no server UID; queueing an op for it would
    guarantee a permanent failure."""
    from sqlalchemy import select

    import app.tasks.email_sync as sync

    monkeypatch.setattr(sync.push_ops, "apply_async", lambda *a, **k: None)
    thread, msg = synced_mailbox
    msg.imap_uid = None
    await db_session.commit()

    await client.post("/api/email/threads/actions",
                      json={"thread_ids": [str(thread.id)], "action": "read"})
    ops = (await db_session.execute(select(EmailSyncOp))).scalars().all()
    assert ops == []


class _FakeIMAP:
    """Minimal stand-in for imaplib.IMAP4 — enough for sync_flags."""

    def __init__(self, flags_by_uid=None, fail_select=()):
        self.untagged_responses = {"UIDVALIDITY": [b"777"], "UIDNEXT": [b"9"]}
        self._flags = flags_by_uid or {}
        self._fail_select = set(fail_select)
        self.selected = None

    def select(self, name, readonly=False):
        clean = name.strip('"')
        if clean in self._fail_select:
            return "NO", [b"nope"]
        self.selected = clean
        return "OK", [b"3"]

    def uid(self, command, *args):
        if command == "fetch":
            out = []
            for uid, flags in self._flags.items():
                joined = " ".join(flags).encode()
                out.append((b"1 (UID %d FLAGS (%s))" % (uid, joined), b""))
            return "OK", out
        return "OK", []

    def logout(self):
        return "BYE", []


def test_an_empty_folder_is_not_left_marked_as_failing(test_engine, monkeypatch):
    """A folder with nothing in it returns early; clearing the error only at
    the end of the block left it permanently "select failed" on the live box
    even though the SELECT had succeeded."""
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    import app.tasks.email_sync as sync

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    monkeypatch.setattr("app.db.sync_session.sync_session", lambda: Session(engine))
    monkeypatch.setattr(sync, "_connect", lambda config: _FakeIMAP())

    try:
        with Session(engine) as db:
            db.add(MailboxConfig(
                name="syncbox", display_name="Sync", imap_host="m.example.com",
                imap_port=993, imap_user="syncbox", imap_password_encrypted="x",
                imap_ssl=True, is_active=True,
            ))
            db.add(MailboxFolder(
                mailbox="syncbox", remote_name="INBOX", local_folder="inbox",
                sync_enabled=True, sync_error="select failed",
            ))
            db.commit()

        result = sync.sync_flags.apply(args=["syncbox"]).get()
        assert result["status"] == "ok"

        with Session(engine) as db:
            row = db.query(MailboxFolder).filter_by(mailbox="syncbox").one()
            assert row.sync_error is None, "успешный SELECT должен снимать прошлую ошибку"
            assert row.last_sync_at is not None
            assert row.uid_validity == 777
    finally:
        with Session(engine) as db:
            db.execute(delete(MailboxFolder).where(MailboxFolder.mailbox == "syncbox"))
            db.execute(delete(MailboxConfig).where(MailboxConfig.name == "syncbox"))
            db.commit()
        engine.dispose()


def test_server_side_flag_changes_come_back_to_us(test_engine, monkeypatch):
    """The missing half of the loop: reading a letter in Thunderbird left this
    client showing it unread indefinitely."""
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    import app.tasks.email_sync as sync

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    monkeypatch.setattr("app.db.sync_session.sync_session", lambda: Session(engine))
    monkeypatch.setattr(
        sync, "_connect",
        lambda config: _FakeIMAP(flags_by_uid={41: ["\\Seen", "\\Flagged"]}),
    )

    ids = {}
    try:
        with Session(engine) as db:
            db.add(MailboxConfig(
                name="syncbox2", display_name="Sync", imap_host="m.example.com",
                imap_port=993, imap_user="syncbox2", imap_password_encrypted="x",
                imap_ssl=True, is_active=True,
            ))
            db.add(MailboxFolder(
                mailbox="syncbox2", remote_name="INBOX", local_folder="inbox",
                sync_enabled=True,
            ))
            thread = EmailThread(subject="Письмо", mailbox="syncbox2", message_count=1,
                                 is_read=False, unread_count=1)
            msg = EmailMessage(
                thread=thread, mailbox="syncbox2", subject="Письмо",
                from_address="x@y.example", to_addresses=["syncbox2@example.com"],
                received_at=datetime.now(timezone.utc),
                message_id_header=f"<{uuid.uuid4()}@y.example>",
                imap_uid=41, imap_folder="INBOX", is_read=False, is_starred=False,
            )
            db.add_all([thread, msg])
            db.commit()
            ids = {"msg": msg.id, "thread": thread.id}

        result = sync.sync_flags.apply(args=["syncbox2"]).get()
        assert result["updated"] == 1

        with Session(engine) as db:
            msg = db.get(EmailMessage, ids["msg"])
            assert msg.is_read is True and msg.is_starred is True
            assert msg.flags_synced_at is not None
            # Thread state follows its messages, not the other way round.
            thread = db.get(EmailThread, ids["thread"])
            assert thread.is_read is True and thread.unread_count == 0
    finally:
        with Session(engine) as db:
            if ids:
                db.execute(delete(EmailMessage).where(EmailMessage.id == ids["msg"]))
                db.execute(delete(EmailThread).where(EmailThread.id == ids["thread"]))
            db.execute(delete(MailboxFolder).where(MailboxFolder.mailbox == "syncbox2"))
            db.execute(delete(MailboxConfig).where(MailboxConfig.name == "syncbox2"))
            db.commit()
        engine.dispose()


# ── Ф2.4: our own Sent folder on the server ────────────────────────────────


def test_append_parses_the_assigned_uid():
    """APPENDUID makes our outbound copy addressable; without a UID it would be
    the one message in the mailbox we can never tell the server anything about."""
    from app.domain.imap_sync import append_to_folder

    class _Conn:
        def __init__(self, reply):
            self.reply = reply
            self.calls = []

        def append(self, folder, flags, stamp, raw):
            self.calls.append((folder, flags, raw))
            return self.reply

    conn = _Conn(("OK", [b"[APPENDUID 1787859013 42] Append completed"]))
    assert append_to_folder(conn, "Sent", b"raw message") == 42
    assert conn.calls[0][0] == '"Sent"'
    assert "\\Seen" in conn.calls[0][1]

    # A server that does not report APPENDUID is not a failure.
    assert append_to_folder(_Conn(("OK", [b"Append completed"])), "Sent", b"x") is None


def test_append_failure_is_raised_not_swallowed():
    """The caller decides what to do; silently "succeeding" would leave the
    Sent folder empty with nobody the wiser."""
    from app.domain.imap_sync import append_to_folder

    class _Conn:
        def append(self, *a):
            return "NO", [b"quota exceeded"]

    with pytest.raises(RuntimeError, match="APPEND"):
        append_to_folder(_Conn(), "Sent", b"x")


async def test_outbound_copy_records_its_server_uid(db_session, synced_mailbox):
    from app.db.models import MailboxFolder
    from app.domain.email_thread import record_outbound_message

    db_session.add(MailboxFolder(
        mailbox="procurement", remote_name="Sent", local_folder="sent",
        special_use="\\Sent", sync_enabled=True,
    ))
    await db_session.commit()

    msg = await record_outbound_message(
        db_session,
        mailbox="procurement",
        draft_data={"to_addresses": ["x@y.example"], "subject": "Ответ",
                    "body_text": "текст"},
        smtp_message_id="<out-1@ourfirm.example>",
        from_address="zakupki@ourfirm.example",
        imap_uid=99,
    )
    await db_session.commit()

    assert msg.imap_uid == 99
    # …and the folder, or flag sync would never find it.
    assert msg.imap_folder == "Sent"


# ── Ф2.3: IDLE, and what happens without it ────────────────────────────────


def test_idle_degrades_to_polling_instead_of_failing(monkeypatch):
    """A deployment without imapclient must keep working: polling is the safety
    net, and "no IDLE" is a latency property, not an outage."""
    from app.tasks import email_idle

    monkeypatch.setattr(email_idle, "_idle_supported", lambda: False)

    assert email_idle.idle_dispatch.apply().get() == {
        "status": "skipped", "reason": "imapclient_not_installed",
    }
    result = email_idle.idle_watch.apply(args=["procurement"]).get()
    assert result["status"] == "skipped"


def test_idle_dispatch_leases_one_watcher_per_mailbox(test_engine, monkeypatch):
    """A duplicate dispatch must not open a second IDLE connection to the same
    mailbox — servers limit concurrent connections, and two watchers would
    double every poll they trigger."""
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    from app.tasks import email_idle

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    monkeypatch.setattr("app.db.sync_session.sync_session", lambda: Session(engine))
    monkeypatch.setattr(email_idle, "_idle_supported", lambda: True)

    launched: list[str] = []
    monkeypatch.setattr(
        email_idle.idle_watch, "apply_async",
        lambda args=None, **k: launched.append(args[0]),
    )

    leases: dict = {}

    class _Redis:
        def set(self, key, value, nx=False, ex=None):
            if nx and key in leases:
                return False
            leases[key] = value
            return True

    monkeypatch.setattr("app.utils.redis_client.get_sync_redis", lambda: _Redis())

    try:
        with Session(engine) as db:
            db.add(MailboxConfig(
                name="idlebox", display_name="Idle", imap_host="m.example.com",
                imap_port=993, imap_user="idlebox", imap_password_encrypted="x",
                imap_ssl=True, is_active=True,
            ))
            db.commit()

        first = email_idle.idle_dispatch.apply().get()
        assert "idlebox" in first["started"]

        second = email_idle.idle_dispatch.apply().get()
        assert second["started"] == []          # lease still held
        assert launched == ["idlebox"]
    finally:
        with Session(engine) as db:
            db.execute(delete(MailboxConfig).where(MailboxConfig.name == "idlebox"))
            db.commit()
        engine.dispose()


def test_a_subfolder_of_inbox_is_treated_as_inbox():
    """Ф2 — находка с живого стенда: mail.ru кладёт письмо самому себе в
    INBOX/ToMyself, провайдеры автосортируют в INBOX/Newsletters. Такая папка
    оставалась неназначенной и несинкаемой — письма есть на сервере и нет
    здесь. Самая тихая из возможных потерь."""
    from app.domain.imap_sync import RemoteFolder

    assert RemoteFolder(
        name="INBOX/ToMyself", flags=(), delimiter="/"
    ).local_folder == "inbox"
    assert RemoteFolder(
        name="INBOX.Newsletters", flags=(), delimiter="."
    ).local_folder == "inbox"
    # Спец-папки и привычные имена по-прежнему сильнее, чем «дочерняя INBOX».
    assert RemoteFolder(
        name="INBOX/Sent", flags=(), delimiter="/"
    ).local_folder == "sent"
    assert RemoteFolder(
        name="INBOX/Trash", flags=("\\Trash",), delimiter="/", special_use="\\Trash"
    ).local_folder == "trash"
    # Папка верхнего уровня, которую мы не узнали, остаётся неназначенной:
    # угадывать, что «Work» это входящие, нельзя.
    assert RemoteFolder(name="Work", flags=(), delimiter="/").local_folder is None
    # Невыбираемый контейнер письмами не бывает.
    assert RemoteFolder(
        name="INBOX/Archive2024", flags=("\\Noselect",), delimiter="/", selectable=False
    ).local_folder is None


async def test_an_unrecognised_folder_can_be_mapped_by_hand(client, db_session):
    """Ф2.1 — «маппинг серверная папка → наша настраивается в UI ящика»:
    эндпоинта для этого не было вовсе."""
    from app.db.models import MailboxConfig, MailboxFolder

    db_session.add(MailboxConfig(
        name="mapbox", imap_host="m.example.com", imap_port=993, imap_user="mapbox",
        imap_password_encrypted="x", imap_ssl=True, is_active=True,
    ))
    folder = MailboxFolder(
        mailbox="mapbox", remote_name="Work/Suppliers", local_folder=None,
        sync_enabled=False,
    )
    db_session.add(folder)
    await db_session.commit()

    # Синк без назначения — это no-op, который выглядит работающим.
    resp = await client.patch(
        f"/api/mailbox/folders/{folder.id}", json={"sync_enabled": True},
    )
    assert resp.status_code == 422, resp.text

    resp = await client.patch(
        f"/api/mailbox/folders/{folder.id}",
        json={"local_folder": "inbox", "sync_enabled": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["local_folder"] == "inbox"
    assert resp.json()["sync_enabled"] is True

    resp = await client.patch(
        f"/api/mailbox/folders/{folder.id}", json={"local_folder": "мусорка"},
    )
    assert resp.status_code == 422

    # Снятие назначения выключает и синк.
    resp = await client.patch(
        f"/api/mailbox/folders/{folder.id}", json={"local_folder": None},
    )
    assert resp.json()["sync_enabled"] is False


def test_a_folder_name_with_a_space_is_quoted_for_select():
    """Живая находка: у mail.ru есть папка «INBOX/Public services». Без кавычек
    пробел делает из неё два аргумента, и сервер отвечает
    ``BAD [CLIENTBUG] Invalid number of arguments`` — папка не читается."""
    from app.tasks.imap_client import _quoted_folder

    assert _quoted_folder("INBOX") == "INBOX"
    assert _quoted_folder("INBOX/Public services") == '"INBOX/Public services"'
    # Кириллица приходит в modified UTF-7 и спецсимволов не содержит.
    assert _quoted_folder("&BCEEPwQwBDw-") == "&BCEEPwQwBDw-"
    # Повторное закавычивание сломало бы имя.
    assert _quoted_folder('"INBOX/Already quoted"') == '"INBOX/Already quoted"'
    assert _quoted_folder('weird"name') == '"weird\\"name"'
