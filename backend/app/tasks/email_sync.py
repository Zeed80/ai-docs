"""Two-way IMAP synchronisation — Ф2.

Three jobs, deliberately separate so one failing does not stall the others:

* ``email.discover_folders`` — what folders this mailbox actually has;
* ``email.sync_flags``       — what changed on the SERVER (read elsewhere,
  moved by hand, deleted from a phone);
* ``email.push_ops``         — what changed HERE and has to reach the server.

Before this, state flowed one way only: the poller read one folder and wrote
to Postgres, and every action in our UI stopped there. Archiving a letter in
this client left it unread in Outlook forever.
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select

from app.core.metrics import (
    email_imap_errors_total,
    email_sync_ops_pending,
    email_sync_ops_total,
)
from app.tasks.celery_app import celery_app

from app.domain.email_counts import invalidate_mailbox_counts_sync

logger = structlog.get_logger()

_MAX_OP_ATTEMPTS = 5


def _connect(config):
    """Authenticated IMAP connection for a mailbox config (password or OAuth)."""
    from app.config import settings

    timeout = settings.imap_timeout_seconds
    if config.imap_ssl:
        conn = imaplib.IMAP4_SSL(config.imap_host, config.imap_port, timeout=timeout)
    else:
        conn = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=timeout)

    if (config.auth_method or "password") == "oauth2":
        from app.db.sync_session import sync_session
        from app.domain.oauth_mail import (
            get_valid_access_token_sync,
            imap_xoauth2_authobject,
        )

        with sync_session() as db:
            from app.db.models import MailboxConfig

            row = db.execute(
                select(MailboxConfig).where(MailboxConfig.name == config.name)
            ).scalar_one()
            token = get_valid_access_token_sync(db, row)
        conn.authenticate("XOAUTH2", imap_xoauth2_authobject(config.imap_user, token))
    else:
        from app.utils.crypto import decrypt_password

        conn.login(config.imap_user, decrypt_password(config.imap_password_encrypted))
    return conn


@celery_app.task(name="email.discover_folders", bind=True, max_retries=2, queue="mail")
def discover_folders(self, mailbox: str) -> dict:
    """Record what folders the server actually has.

    The live stand made the case for this: mail.ru files a message you send to
    yourself into ``INBOX/ToMyself``, and a poller that reads one configured
    folder never sees it. Folders are recorded, not auto-enabled — syncing
    someone's entire archive on a whim is not a decision this task should make.
    """
    from app.db.models import MailboxConfig, MailboxFolder
    from app.db.sync_session import sync_session
    from app.domain.imap_sync import decode_mailbox_name, parse_list_response

    with sync_session() as db:
        config = db.execute(
            select(MailboxConfig).where(
                MailboxConfig.name == mailbox,
                MailboxConfig.is_active == True,  # noqa: E712
            )
        ).scalar_one_or_none()
        if config is None:
            return {"status": "error", "reason": "mailbox_not_configured"}

        try:
            conn = _connect(config)
        except Exception as exc:  # noqa: BLE001
            logger.error("imap_discover_connect_failed", mailbox=mailbox, error=str(exc))
            raise self.retry(exc=exc, countdown=120)

        try:
            status, lines = conn.list()
            if status != "OK":
                return {"status": "error", "reason": "list_failed"}
            remote = parse_list_response(lines)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

        existing = {
            row.remote_name: row
            for row in db.execute(
                select(MailboxFolder).where(MailboxFolder.mailbox == mailbox)
            ).scalars().all()
        }

        created = 0
        for folder in remote:
            decoded = decode_mailbox_name(folder.name)
            row = existing.get(folder.name) or existing.get(decoded)
            if row is None:
                row = MailboxFolder(
                    mailbox=mailbox,
                    remote_name=folder.name,
                    local_folder=folder.local_folder,
                    special_use=folder.special_use,
                    is_selectable=folder.selectable,
                    # Only the folders we can name are synced by default; the
                    # rest are recorded so an admin can switch them on.
                    sync_enabled=folder.local_folder is not None,
                )
                db.add(row)
                created += 1
            else:
                # A folder that was unmapped and now resolves to one of ours
                # starts syncing: it was not "switched off", it was unknown.
                # A folder someone deliberately disabled keeps its setting.
                newly_mapped = row.local_folder is None and folder.local_folder
                row.local_folder = folder.local_folder or row.local_folder
                row.special_use = folder.special_use or row.special_use
                row.is_selectable = folder.selectable
                if newly_mapped:
                    row.sync_enabled = True
        db.commit()

    logger.info("imap_folders_discovered", mailbox=mailbox, total=len(remote), created=created)
    return {"status": "ok", "folders": len(remote), "created": created}


@celery_app.task(name="email.push_ops", bind=True, max_retries=0, queue="mail")
def push_ops(self, mailbox: str | None = None, limit: int = 200) -> dict:
    """Apply locally-made changes to the server.

    Idempotent by construction: every op re-reads the message's current server
    flags first and skips what is already true, so a retry after a partial
    failure cannot double-apply anything.
    """
    from app.db.models import EmailMessage, EmailSyncOp, MailboxConfig
    from app.db.sync_session import sync_session

    applied = 0
    failed = 0
    with sync_session() as db:
        query = select(EmailSyncOp).where(EmailSyncOp.state == "pending")
        if mailbox:
            query = query.where(EmailSyncOp.mailbox == mailbox)
        ops = db.execute(
            query.order_by(EmailSyncOp.created_at.asc()).limit(limit)
        ).scalars().all()
        if not ops:
            return {"status": "ok", "applied": 0, "failed": 0}

        by_mailbox: dict[str, list] = {}
        for op in ops:
            by_mailbox.setdefault(op.mailbox, []).append(op)

        for box, box_ops in by_mailbox.items():
            config = db.execute(
                select(MailboxConfig).where(
                    MailboxConfig.name == box,
                    MailboxConfig.is_active == True,  # noqa: E712
                )
            ).scalar_one_or_none()
            if config is None:
                for op in box_ops:
                    op.state = "failed"
                    op.last_error = "mailbox not configured"
                    failed += 1
                continue

            try:
                conn = _connect(config)
            except Exception as exc:  # noqa: BLE001
                logger.error("imap_push_connect_failed", mailbox=box, error=str(exc))
                email_imap_errors_total.labels(mailbox=box, stage="push").inc()
                for op in box_ops:
                    op.attempts += 1
                    op.last_error = str(exc)[:300]
                    if op.attempts >= _MAX_OP_ATTEMPTS:
                        op.state = "failed"
                    failed += 1
                continue

            try:
                selected: str | None = None
                for op in box_ops:
                    msg = (
                        db.get(EmailMessage, op.message_id) if op.message_id else None
                    )
                    if msg is None or not msg.imap_uid or not msg.imap_folder:
                        # Nothing to say to the server about a message we never
                        # got a UID for — mark it done rather than retrying
                        # forever against a message that cannot be addressed.
                        op.state = "skipped"
                        op.last_error = "no imap uid"
                        continue

                    folder = msg.imap_folder
                    if folder != selected:
                        status, _ = conn.select(f'"{folder}"')
                        if status != "OK":
                            op.attempts += 1
                            op.last_error = f"select {folder} failed"
                            failed += 1
                            continue
                        selected = folder

                    try:
                        _apply_one(conn, db, op, msg)
                        email_sync_ops_total.labels(op=op.op, outcome="done").inc()
                        op.state = "done"
                        op.applied_at = datetime.now(timezone.utc)
                        applied += 1
                    except Exception as exc:  # noqa: BLE001
                        op.attempts += 1
                        op.last_error = str(exc)[:300]
                        if op.attempts >= _MAX_OP_ATTEMPTS:
                            op.state = "failed"
                            email_sync_ops_total.labels(
                                op=op.op, outcome="failed"
                            ).inc()
                            logger.error(
                                "imap_op_gave_up", op=op.op, mailbox=box,
                                message_id=str(op.message_id), error=str(exc),
                            )
                        failed += 1
            finally:
                try:
                    conn.logout()
                except Exception:  # noqa: BLE001
                    pass
        db.commit()

    # The number that matters operationally: how far behind the server we are.
    with sync_session() as db:
        pending = db.execute(
            select(func.count(EmailSyncOp.id)).where(EmailSyncOp.state == "pending")
        ).scalar() or 0
    email_sync_ops_pending.set(pending)

    logger.info("imap_ops_pushed", applied=applied, failed=failed, pending=pending)
    return {"status": "ok", "applied": applied, "failed": failed, "pending": pending}


def _apply_one(conn, db, op, msg) -> None:
    """Apply a single queued operation to the selected folder."""
    from app.domain.imap_sync import parse_flags_response

    uid = str(msg.imap_uid).encode()

    # Re-read what the server currently thinks: this is what makes a retry
    # safe, and it is also how a conflict (someone already deleted the letter)
    # surfaces as "nothing to do" rather than an error.
    status, data = conn.uid("fetch", uid, "(FLAGS)")
    current = parse_flags_response(data).get(msg.imap_uid, set()) if status == "OK" else set()

    if op.op == "seen" and "\\Seen" not in current:
        conn.uid("store", uid, "+FLAGS", "\\Seen")
    elif op.op == "unseen" and "\\Seen" in current:
        conn.uid("store", uid, "-FLAGS", "\\Seen")
    elif op.op == "flagged" and "\\Flagged" not in current:
        conn.uid("store", uid, "+FLAGS", "\\Flagged")
    elif op.op == "unflagged" and "\\Flagged" in current:
        conn.uid("store", uid, "-FLAGS", "\\Flagged")
    elif op.op in ("move", "delete"):
        target = (op.payload or {}).get("remote_folder")
        if not target:
            raise ValueError("move without a target folder")
        # `target` comes from mailbox_folders.remote_name — wire form already.
        result, _ = conn.uid("copy", uid, f'"{target}"')
        if result != "OK":
            raise RuntimeError(f"copy to {target} failed")
        conn.uid("store", uid, "+FLAGS", "\\Deleted")
        conn.expunge()
        msg.imap_folder = target
        msg.imap_uid = None          # the UID belongs to the old folder


@celery_app.task(name="email.sync_flags", bind=True, max_retries=2, queue="mail")
def sync_flags(self, mailbox: str, folder: str | None = None, window: int = 500) -> dict:
    """Bring changes made ON THE SERVER back to us.

    The missing half of the loop: reading a letter in Thunderbird, starring it
    on a phone or deleting it from webmail left this client showing the old
    state indefinitely. Local edits are not overwritten blindly — a message
    with a pending push op is skipped, so the two directions cannot fight.
    """
    from app.db.models import EmailMessage, EmailSyncOp, MailboxConfig, MailboxFolder
    from app.db.sync_session import sync_session
    from app.domain.imap_sync import parse_flags_response, parse_select_response

    updated = 0
    with sync_session() as db:
        config = db.execute(
            select(MailboxConfig).where(
                MailboxConfig.name == mailbox,
                MailboxConfig.is_active == True,  # noqa: E712
            )
        ).scalar_one_or_none()
        if config is None:
            return {"status": "error", "reason": "mailbox_not_configured"}

        folders_q = select(MailboxFolder).where(
            MailboxFolder.mailbox == mailbox,
            MailboxFolder.sync_enabled == True,  # noqa: E712
            MailboxFolder.is_selectable == True,  # noqa: E712
        )
        if folder:
            folders_q = folders_q.where(MailboxFolder.remote_name == folder)
        folders = db.execute(folders_q).scalars().all()
        if not folders:
            return {"status": "ok", "updated": 0, "note": "нет папок для синхронизации"}

        try:
            conn = _connect(config)
        except Exception as exc:  # noqa: BLE001
            raise self.retry(exc=exc, countdown=120)

        try:
            for row in folders:
                try:
                    # remote_name is stored exactly as the server sent it in
                    # LIST — already modified UTF-7. Encoding it a second time
                    # turns "&BCEEPwQwBDw-" into "&-BCEEPwQwBDw-" and every
                    # SELECT of a Cyrillic folder fails (seen on the live box).
                    status, data = conn.select(f'"{row.remote_name}"', readonly=True)
                    if status != "OK":
                        row.sync_error = "select failed"
                        continue
                    meta = parse_select_response(data, conn.untagged_responses)

                    # A reissued UIDVALIDITY means every stored UID now points
                    # at a different message. Continuing would silently apply
                    # our actions to someone else's mail.
                    if (
                        row.uid_validity is not None
                        and meta.get("uid_validity") is not None
                        and meta["uid_validity"] != row.uid_validity
                    ):
                        logger.warning(
                            "imap_uidvalidity_changed", mailbox=mailbox,
                            folder=row.remote_name, was=row.uid_validity,
                            now=meta["uid_validity"],
                        )
                        db.query(EmailMessage).filter(
                            EmailMessage.mailbox == mailbox,
                            EmailMessage.imap_folder == row.remote_name,
                        ).update({"imap_uid": None}, synchronize_session=False)
                        row.last_seen_uid = 0
                    row.uid_validity = meta.get("uid_validity", row.uid_validity)
                    row.uid_next = meta.get("uid_next", row.uid_next)
                    row.highest_modseq = meta.get("highest_modseq", row.highest_modseq)
                    # Clear the error as soon as the folder opens, not at the
                    # end of the block: an empty folder returns early below and
                    # would otherwise stay marked as failing forever.
                    row.sync_error = None
                    row.last_sync_at = datetime.now(timezone.utc)

                    known = db.execute(
                        select(EmailMessage.id, EmailMessage.imap_uid,
                               EmailMessage.is_read, EmailMessage.is_starred)
                        .where(
                            EmailMessage.mailbox == mailbox,
                            EmailMessage.imap_folder == row.remote_name,
                            EmailMessage.imap_uid.isnot(None),
                        )
                        .order_by(EmailMessage.imap_uid.desc())
                        .limit(window)
                    ).all()
                    if not known:
                        continue

                    uids = sorted(int(u) for _, u, _, _ in known)
                    rng = f"{uids[0]}:{uids[-1]}".encode()
                    status, data = conn.uid("fetch", rng, "(FLAGS)")
                    if status != "OK":
                        continue
                    server_flags = parse_flags_response(data)

                    pending = {
                        op.message_id
                        for op in db.execute(
                            select(EmailSyncOp).where(
                                EmailSyncOp.state == "pending",
                                EmailSyncOp.mailbox == mailbox,
                            )
                        ).scalars().all()
                    }

                    for msg_id, uid, is_read, is_starred in known:
                        if msg_id in pending:
                            continue          # our own change is still in flight
                        flags = server_flags.get(int(uid))
                        if flags is None:
                            continue
                        seen = "\\Seen" in flags
                        starred = "\\Flagged" in flags
                        if seen == bool(is_read) and starred == bool(is_starred):
                            continue
                        msg = db.get(EmailMessage, msg_id)
                        if msg is None:
                            continue
                        msg.is_read = seen
                        msg.is_starred = starred
                        msg.flags_synced_at = datetime.now(timezone.utc)
                        updated += 1

                except Exception as exc:  # noqa: BLE001
                    row.sync_error = str(exc)[:300]
                    email_imap_errors_total.labels(mailbox=mailbox, stage="flags").inc()
                    logger.warning(
                        "imap_folder_sync_failed", mailbox=mailbox,
                        folder=row.remote_name, error=str(exc),
                    )
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass

        if updated:
            _refresh_thread_read_state(db, mailbox)
        db.commit()
    if updated:
        invalidate_mailbox_counts_sync()

    logger.info("imap_flags_synced", mailbox=mailbox, updated=updated)
    return {"status": "ok", "updated": updated}


def _refresh_thread_read_state(db, mailbox: str) -> None:
    """Thread-level read state follows its messages, not the other way round."""
    from sqlalchemy import func

    from app.db.models import EmailMessage, EmailThread

    rows = db.execute(
        select(
            EmailMessage.thread_id,
            func.count(EmailMessage.id).filter(EmailMessage.is_read == False),  # noqa: E712
        )
        .where(EmailMessage.mailbox == mailbox, EmailMessage.thread_id.isnot(None))
        .group_by(EmailMessage.thread_id)
    ).all()
    for thread_id, unread in rows:
        thread = db.get(EmailThread, thread_id)
        if thread is None:
            continue
        thread.unread_count = int(unread or 0)
        thread.is_read = not unread


@celery_app.task(name="email.sync_flags_all", bind=True, queue="mail")
def sync_flags_all(self) -> dict:
    """Beat entrypoint: fan out sync_flags per active mailbox."""
    from app.db.models import MailboxConfig
    from app.db.sync_session import sync_session

    with sync_session() as db:
        names = list(db.execute(
            select(MailboxConfig.name).where(
                MailboxConfig.is_active == True  # noqa: E712
            )
        ).scalars().all())
    for name in names:
        sync_flags.apply_async(args=[name], queue="mail")
    return {"status": "ok", "dispatched": names}


@celery_app.task(name="email.discover_folders_all", bind=True, queue="mail")
def discover_folders_all(self) -> dict:
    """Beat entrypoint: refresh the folder map of every active mailbox."""
    from app.db.models import MailboxConfig
    from app.db.sync_session import sync_session

    with sync_session() as db:
        names = list(db.execute(
            select(MailboxConfig.name).where(
                MailboxConfig.is_active == True  # noqa: E712
            )
        ).scalars().all())
    for name in names:
        discover_folders.apply_async(args=[name], queue="mail")
    return {"status": "ok", "dispatched": names}
