"""IMAP IDLE — new mail in seconds instead of on the next poll (Ф2.3).

The poller runs every five minutes, so a letter could sit unseen for that long
and the client felt like a news feed rather than a mail client. IDLE lets the
server tell us the moment something arrives.

Deliberately a supervisor + long-lived watcher rather than a Celery task per
mailbox: an IDLE connection lives for tens of minutes, which is not what a task
queue is for. Polling stays as the safety net — a server without IDLE, a
dropped connection or a crashed watcher must not mean "mail stops arriving".
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

# RFC 2177 says a client must re-issue IDLE at least every 29 minutes.
_IDLE_REFRESH_SECONDS = 25 * 60
# How long one watcher iteration runs before the task returns and is
# rescheduled, so a stuck connection cannot hold a worker slot forever.
_WATCH_BUDGET_SECONDS = 20 * 60


def _idle_supported() -> bool:
    try:
        import imapclient  # noqa: F401

        return True
    except ImportError:
        return False


@celery_app.task(name="email.idle_watch", bind=True, queue="mail")
def idle_watch(self, mailbox: str, folder: str | None = None) -> dict:
    """Hold an IDLE connection and trigger an incremental sync on activity.

    Returns after a bounded budget rather than looping forever; the beat
    dispatcher restarts it. That keeps a hung connection from occupying a
    worker indefinitely — the failure mode a "just loop" implementation has.

    Аренда освобождается ЛЮБЫМ выходом, включая исключение до подключения:
    наблюдатель, упавший на выборе папки, оставлял ключ висеть до конца TTL, и
    ящик оставался без IDLE ещё двадцать минут после починки причины.
    """
    try:
        return _idle_watch_body(mailbox, folder)
    finally:
        _release_lease(mailbox)


def _idle_watch_body(mailbox: str, folder: str | None = None) -> dict:
    if not _idle_supported():
        # Recorded, not raised: the deployment simply polls, which works.
        logger.info("imap_idle_unavailable", mailbox=mailbox)
        return {"status": "skipped", "reason": "imapclient_not_installed"}

    from imapclient import IMAPClient

    from app.db.models import MailboxConfig, MailboxFolder
    from app.db.sync_session import sync_session
    with sync_session() as db:
        config = db.execute(
            select(MailboxConfig).where(
                MailboxConfig.name == mailbox,
                MailboxConfig.is_active == True,  # noqa: E712
            )
        ).scalar_one_or_none()
        if config is None:
            return {"status": "error", "reason": "mailbox_not_configured"}
        # Во «Входящие» отображается НЕСКОЛЬКО серверных папок: подпапки
        # INBOX, куда провайдер сам раскладывает почту (INBOX/ToMyself,
        # INBOX/Newsletters). scalar_one_or_none() на таком наборе падал с
        # MultipleResultsFound и убивал наблюдателя на каждом запуске.
        # Следим за основной папкой ящика: IDLE держит одно соединение, и
        # событие в ней — самый частый повод пересинхронизироваться.
        watch_folder = folder
        if not watch_folder:
            primary = (config.imap_folder or "INBOX").strip()
            candidates = db.execute(
                select(MailboxFolder.remote_name).where(
                    MailboxFolder.mailbox == mailbox,
                    MailboxFolder.local_folder == "inbox",
                )
            ).scalars().all()
            watch_folder = (
                primary if primary in candidates
                else (candidates[0] if candidates else primary)
            )
        password = None
        if (config.auth_method or "password") != "oauth2":
            from app.utils.crypto import decrypt_password

            password = decrypt_password(config.imap_password_encrypted)

    started = time.monotonic()
    events = 0
    try:
        client = IMAPClient(config.imap_host, port=config.imap_port, ssl=config.imap_ssl)
        if password is None:
            from app.db.sync_session import sync_session as _s
            from app.domain.oauth_mail import get_valid_access_token_sync

            with _s() as db:
                row = db.execute(
                    select(MailboxConfig).where(MailboxConfig.name == mailbox)
                ).scalar_one()
                token = get_valid_access_token_sync(db, row)
            client.oauth2_login(config.imap_user, token)
        else:
            client.login(config.imap_user, password)
    except Exception as exc:  # noqa: BLE001
        logger.warning("imap_idle_connect_failed", mailbox=mailbox, error=str(exc))
        return {"status": "error", "reason": str(exc)[:200]}

    try:
        client.select_folder(watch_folder, readonly=True)
        while time.monotonic() - started < _WATCH_BUDGET_SECONDS:
            client.idle()
            try:
                responses = client.idle_check(timeout=min(
                    _IDLE_REFRESH_SECONDS,
                    max(30, _WATCH_BUDGET_SECONDS - (time.monotonic() - started)),
                ))
            finally:
                client.idle_done()

            if not responses:
                continue          # timeout: re-issue IDLE, nothing arrived
            events += 1
            logger.info("imap_idle_activity", mailbox=mailbox, folder=watch_folder)
            _on_activity(mailbox)
    except Exception as exc:  # noqa: BLE001
        logger.warning("imap_idle_failed", mailbox=mailbox, error=str(exc))
        return {"status": "error", "events": events, "reason": str(exc)[:200]}
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass

    return {"status": "ok", "events": events}


def _release_lease(mailbox: str) -> None:
    """Отдать аренду сразу, а не ждать истечения TTL.

    Наблюдатель, упавший с исключением, оставлял ключ висеть до конца TTL —
    и ящик оставался без IDLE ещё двадцать минут после того, как причина
    падения уже исправлена. TTL остаётся страховкой на случай, когда процесс
    убит и до этого кода дело не дошло.
    """
    try:
        from app.utils.redis_client import get_sync_redis

        get_sync_redis().delete(f"email:idle:{mailbox}")
    except Exception as exc:  # noqa: BLE001
        logger.debug("imap_idle_lease_release_failed", mailbox=mailbox, error=str(exc))


def _on_activity(mailbox: str) -> None:
    """Something happened in the folder: fetch it now instead of in 5 minutes."""
    from app.tasks.ingest import poll_imap_mailbox

    poll_imap_mailbox.apply_async(args=[mailbox], queue="mail")


@celery_app.task(name="email.idle_dispatch", bind=True, queue="mail")
def idle_dispatch(self) -> dict:
    """Beat entrypoint: keep one watcher per mailbox alive.

    Idempotence is by Redis lease rather than bookkeeping: a watcher holds a
    short-lived key while it runs, so a duplicate dispatch is a no-op instead
    of a second connection to the same mailbox.
    """
    if not _idle_supported():
        return {"status": "skipped", "reason": "imapclient_not_installed"}

    from app.db.models import MailboxConfig
    from app.db.sync_session import sync_session
    from app.utils.redis_client import get_sync_redis

    with sync_session() as db:
        names = list(db.execute(
            select(MailboxConfig.name).where(
                MailboxConfig.is_active == True  # noqa: E712
            )
        ).scalars().all())

    started: list[str] = []
    try:
        redis = get_sync_redis()
    except Exception:  # noqa: BLE001
        redis = None

    for name in names:
        if redis is not None:
            key = f"email:idle:{name}"
            # Lease slightly longer than the watcher's budget.
            if not redis.set(key, str(datetime.now(timezone.utc)), nx=True,
                             ex=_WATCH_BUDGET_SECONDS + 120):
                continue
        idle_watch.apply_async(args=[name], queue="mail")
        started.append(name)

    return {"status": "ok", "started": started}
