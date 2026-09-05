"""Общий ящик тоже опрашивается по водяному знаку UID, а не по флагу \\Seen.

Найдено на живом сервере: три отправленных письма лежали в INBOX/ToMyself,
но у нас их не было. Причина — поиск UNSEEN: провайдер сам помечает
прочитанным письмо самому себе. Тот же механизм теряет любое письмо, которое
человек открыл в другом клиенте раньше нашего опроса — навсегда и без ошибки.

Главное здесь — не то, что письма находятся, а то, что переход на новый
критерий НЕ приводит к повторному ингесту всей истории.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session


@pytest.fixture
def sync_db(test_engine, monkeypatch):
    """Синхронная сессия с настоящим commit — путь ингеста синхронный.

    Убираем за собой только свои строки: сносить таблицы целиком значит лезть
    за пределы пер-тестовой транзакции, на которой держится остальной набор.
    """
    from app.db.models import EmailMessage, EmailThread, MailboxConfig, MailboxFolder

    sync_url = test_engine.url.render_as_string(hide_password=False).replace(
        "postgresql+asyncpg://", "postgresql://"
    )
    engine = create_engine(sync_url, pool_pre_ping=True)
    import app.db.sync_session as sync_module

    monkeypatch.setattr(sync_module, "sync_session", lambda: Session(engine))

    boxes = ("wmbox", "wmbox2")
    try:
        with Session(engine) as db:
            yield db
    finally:
        with Session(engine) as db:
            db.execute(delete(EmailMessage).where(EmailMessage.mailbox.in_(boxes)))
            db.execute(delete(EmailThread).where(EmailThread.mailbox.in_(boxes)))
            db.execute(delete(MailboxFolder).where(MailboxFolder.mailbox.in_(boxes)))
            db.execute(delete(MailboxConfig).where(MailboxConfig.name.in_(boxes)))
            db.commit()
        engine.dispose()


def test_watermark_starts_from_what_we_already_have(sync_db):
    """Иначе водяной знак стартовал бы с нуля и ящик переингестился бы
    целиком: дубли в базе поймал бы message_id_header, но правила автоответа
    успели бы отработать на всей истории."""
    from app.db.models import EmailMessage, EmailThread, MailboxConfig
    from app.tasks.imap_client import _known_max_uid

    sync_db.add(
        MailboxConfig(
            name="wmbox",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="wmbox",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
            mailbox_type="shared",
        )
    )
    thread = EmailThread(subject="Т", mailbox="wmbox", message_count=1)
    sync_db.add(thread)
    sync_db.flush()
    for uid, folder in ((17, "INBOX"), (42, "INBOX"), (5, "INBOX/ToMyself")):
        sync_db.add(
            EmailMessage(
                thread_id=thread.id,
                mailbox="wmbox",
                subject=f"uid {uid}",
                from_address="a@b.example",
                to_addresses=["wmbox@example.com"],
                received_at=datetime.now(UTC),
                message_id_header=f"<{folder}-{uid}@b.example>",
                imap_uid=uid,
                imap_folder=folder,
            )
        )
    sync_db.commit()

    assert _known_max_uid("wmbox", "INBOX") == 42
    assert _known_max_uid("wmbox", "INBOX/ToMyself") == 5
    # Незнакомая папка — ноль, и это честно: про неё мы ничего не знаем.
    assert _known_max_uid("wmbox", "INBOX/News") == 0
    assert _known_max_uid("other-box", "INBOX") == 0


def test_folder_state_and_uid_validity_reset(sync_db):
    """Смена UIDVALIDITY означает, что прежние UID указывают на другие письма:
    продолжать с них нельзя, папка переиндексируется."""
    from app.db.models import MailboxFolder
    from app.tasks.imap_client import _folder_state, _save_folder_uid_validity

    sync_db.add(
        MailboxFolder(
            mailbox="wmbox2",
            remote_name="INBOX",
            local_folder="inbox",
            sync_enabled=True,
            uid_validity=1000,
            last_seen_uid=77,
        )
    )
    sync_db.commit()

    assert _folder_state("wmbox2", "INBOX") == (77, 1000)

    _save_folder_uid_validity("wmbox2", "INBOX", 2000)
    sync_db.expire_all()
    assert _folder_state("wmbox2", "INBOX") == (0, 2000), (
        "после смены UIDVALIDITY знак обязан сброситься"
    )

    # Тот же UIDVALIDITY ничего не сбрасывает.
    _save_folder_uid_validity("wmbox2", "INBOX", 2000)
    sync_db.expire_all()
    assert _folder_state("wmbox2", "INBOX") == (0, 2000)


def test_the_seen_flag_is_no_longer_the_selection_criterion():
    """Контракт кода: поиск идёт по UID, флаг остаётся только состоянием
    обработки общего ящика."""
    import inspect

    from app.tasks import imap_client

    src = inspect.getsource(imap_client.fetch_unseen_from_mailbox)
    assert 'conn.uid("search", None, f"UID {watermark + 1}:*")' in src
    assert 'conn.search(None, "UNSEEN")' not in src, (
        "поиск по UNSEEN теряет письма, прочитанные кем-то другим"
    )
    # Флаг всё ещё ставится общему ящику — он его состояние обработки.
    assert "if not personal:" in src and '"+FLAGS"' in src


def test_idle_watcher_survives_several_folders_mapped_to_the_inbox(sync_db):
    """Во «Входящие» отображается несколько серверных папок — подпапки INBOX,
    куда провайдер раскладывает почту сам. Наблюдатель IDLE выбирал папку через
    scalar_one_or_none() и падал с MultipleResultsFound на каждом запуске,
    сразу после того как подпапки INBOX начали отображаться во «Входящие».
    """
    import inspect

    from app.db.models import MailboxConfig, MailboxFolder
    from app.tasks import email_idle

    sync_db.add(
        MailboxConfig(
            name="wmbox",
            imap_host="m.example.com",
            imap_port=993,
            imap_user="wmbox",
            imap_password_encrypted="x",
            imap_ssl=True,
            is_active=True,
            imap_folder="INBOX",
        )
    )
    for remote in ("INBOX", "INBOX/ToMyself", "INBOX/Newsletters"):
        sync_db.add(
            MailboxFolder(
                mailbox="wmbox",
                remote_name=remote,
                local_folder="inbox",
                sync_enabled=True,
            )
        )
    sync_db.commit()

    src = inspect.getsource(email_idle.idle_watch)
    assert "scalar_one_or_none() or config.imap_folder" not in src, (
        "выбор одной папки из нескольких обязан быть явным, а не падать"
    )

    from sqlalchemy import select

    candidates = (
        sync_db.execute(
            select(MailboxFolder.remote_name).where(
                MailboxFolder.mailbox == "wmbox",
                MailboxFolder.local_folder == "inbox",
            )
        )
        .scalars()
        .all()
    )
    assert len(candidates) == 3
    # Основная папка ящика выигрывает у автосортированных подпапок.
    assert "INBOX" in candidates


def test_a_crashed_idle_watcher_releases_its_lease(monkeypatch):
    """Аренда IDLE должна отдаваться любым выходом, включая исключение.

    Упавший наблюдатель оставлял ключ висеть до конца TTL, и ящик оставался
    без IDLE ещё двадцать минут после того, как причина падения уже устранена.
    Так и вышло: наблюдатель падал на выборе папки, а диспетчер молча
    пропускал ящик — «стек здоров, почта не приходит».
    """
    from app.tasks import email_idle

    released: list[str] = []
    monkeypatch.setattr(email_idle, "_release_lease", lambda mb: released.append(mb))

    def _boom(mailbox, folder=None):
        raise RuntimeError("MultipleResultsFound")

    monkeypatch.setattr(email_idle, "_idle_watch_body", _boom)

    try:
        email_idle.idle_watch.apply(args=["boxy"]).get()
    except Exception:
        pass
    assert released == ["boxy"], "аренда не освобождена при падении наблюдателя"

    released.clear()
    monkeypatch.setattr(
        email_idle, "_idle_watch_body", lambda mailbox, folder=None: {"status": "ok"}
    )
    email_idle.idle_watch.apply(args=["boxy"]).get()
    assert released == ["boxy"], "аренда не освобождена при обычном завершении"
