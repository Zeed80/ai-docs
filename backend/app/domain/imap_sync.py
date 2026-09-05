"""IMAP folder discovery and two-way sync primitives — Ф2.

The poller used to read a single folder named in ``MailboxConfig.imap_folder``
and write every action to Postgres only. Two consequences, both confirmed on
the live stand:

* a letter that the server filed anywhere else was invisible — mail.ru puts
  mail you send to yourself into ``INBOX/ToMyself``, and the pipeline simply
  never saw it;
* archiving, reading or deleting here changed nothing on the server, so this
  client and the person's real mail client diverged from the first click.

Everything in this module is deliberately parser-level and side-effect free so
it can be tested without an IMAP server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

# RFC 6154 SPECIAL-USE → our folder vocabulary.
SPECIAL_USE_MAP = {
    "\\Sent": "sent",
    "\\Drafts": "drafts",
    "\\Trash": "trash",
    "\\Junk": "spam",
    "\\Archive": "archive",
    "\\All": None,  # Gmail's "All Mail" duplicates everything
    "\\Flagged": None,
    "\\Important": None,
}

# Servers that do not advertise SPECIAL-USE still use conventional names.
_NAME_HINTS = {
    "sent": ("sent", "отправленные", "sent items", "sent messages"),
    "drafts": ("drafts", "черновики"),
    "trash": ("trash", "deleted", "корзина", "удалённые", "удаленные"),
    "spam": ("spam", "junk", "спам"),
    "archive": ("archive", "архив"),
}

_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+"?(?P<delim>[^"\s]*)"?\s+(?P<name>.*)$')


@dataclass
class RemoteFolder:
    name: str
    flags: tuple[str, ...]
    delimiter: str
    special_use: str | None = None
    selectable: bool = True

    @property
    def local_folder(self) -> str | None:
        """Which of our system folders this is, if any.

        The name is decoded first: servers send folder names in modified UTF-7,
        so a Russian "Корзина" arrives as ``&BBoEPgRABDcEOAQ9BDA-`` and would
        never match a name hint compared raw.
        """
        if self.special_use and self.special_use in SPECIAL_USE_MAP:
            return SPECIAL_USE_MAP[self.special_use]
        decoded = decode_mailbox_name(self.name)
        leaf = (
            decoded.split(self.delimiter)[-1].strip().lower() if self.delimiter else decoded.lower()
        )
        if leaf == "inbox" or decoded.upper() == "INBOX":
            return "inbox"
        for local, hints in _NAME_HINTS.items():
            if leaf in hints:
                return local
        # A sub-folder of INBOX is where the SERVER files incoming mail on its
        # own: mail.ru drops self-addressed letters into INBOX/ToMyself, and
        # providers auto-sort into INBOX/Newsletters, INBOX/Receipts. Leaving
        # those unmapped means those letters exist on the server and never
        # appear here — the quietest possible loss. They belong in the inbox,
        # which is where the person expects to find them.
        parent = decoded.split(self.delimiter)[0].upper() if self.delimiter else ""
        if parent == "INBOX" and self.selectable:
            return "inbox"
        return None


def decode_mailbox_name(raw: str) -> str:
    """IMAP modified UTF-7 → text.

    Folder names come back like ``&BCEEPwQwBDw-`` (that is "Спам"). Leaving
    them encoded means the folder list in settings is unreadable — and a
    Cyrillic name breaks an ASCII search, which is how the live check first
    failed.
    """
    if "&" not in raw:
        return raw
    out: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "&":
            out.append(ch)
            i += 1
            continue
        end = raw.find("-", i)
        if end == -1:
            out.append(ch)
            i += 1
            continue
        chunk = raw[i + 1 : end]
        if not chunk:  # "&-" is a literal ampersand
            out.append("&")
        else:
            try:
                b64 = chunk.replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                import base64

                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:  # noqa: BLE001 — leave anything unparsable as-is
                out.append(raw[i : end + 1])
        i = end + 1
    return "".join(out)


def encode_mailbox_name(name: str) -> str:
    """Text → IMAP modified UTF-7, for SELECT/COPY of a Cyrillic folder."""
    if all(0x20 <= ord(c) <= 0x7E and c != "&" for c in name):
        return name
    import base64

    out: list[str] = []
    buffer: list[str] = []

    def _flush() -> None:
        if not buffer:
            return
        raw = "".join(buffer).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode().rstrip("=").replace("/", ",")
        out.append(f"&{encoded}-")
        buffer.clear()

    for ch in name:
        if ch == "&":
            _flush()
            out.append("&-")
        elif 0x20 <= ord(ch) <= 0x7E:
            _flush()
            out.append(ch)
        else:
            buffer.append(ch)
    _flush()
    return "".join(out)


def parse_list_response(lines) -> list[RemoteFolder]:
    """Parse the raw LIST reply into folders we can reason about."""
    folders: list[RemoteFolder] = []
    for raw in lines or []:
        if raw is None:
            continue
        line = raw if isinstance(raw, bytes) else str(raw).encode()
        match = _LIST_RE.match(line.strip())
        if not match:
            continue
        flags = tuple(f.decode(errors="replace") for f in match.group("flags").split())
        delimiter = match.group("delim").decode(errors="replace")
        name = match.group("name").decode(errors="replace").strip().strip('"')
        special = next((f for f in flags if f in SPECIAL_USE_MAP), None)
        folders.append(
            RemoteFolder(
                name=name,
                flags=flags,
                delimiter=delimiter,
                special_use=special,
                selectable="\\Noselect" not in flags,
            )
        )
    return folders


def parse_select_response(data, untagged: dict | None = None) -> dict:
    """UIDVALIDITY / UIDNEXT / HIGHESTMODSEQ after a SELECT/EXAMINE.

    imaplib's ``select()`` returns only the message count; the interesting
    values arrive as untagged responses and land in ``conn.untagged_responses``.
    Reading them from the return value alone silently produced empty state —
    found on the live mailbox, where every folder ended up with no
    ``uid_validity`` at all.
    """
    out: dict = {}
    for key, field in (
        ("UIDVALIDITY", "uid_validity"),
        ("UIDNEXT", "uid_next"),
        ("HIGHESTMODSEQ", "highest_modseq"),
    ):
        values = (untagged or {}).get(key) or []
        for value in values:
            try:
                out[field] = int(value if isinstance(value, (bytes, str)) else value[0])
                break
            except (TypeError, ValueError, IndexError):
                continue

    for raw in data or []:
        line = raw if isinstance(raw, bytes) else str(raw).encode()
        for key, field in (
            (b"UIDVALIDITY", "uid_validity"),
            (b"UIDNEXT", "uid_next"),
            (b"HIGHESTMODSEQ", "highest_modseq"),
        ):
            match = re.search(key + rb"\s+(\d+)", line)
            if match:
                out[field] = int(match.group(1))
    return out


def parse_flags_response(data) -> dict[int, set[str]]:
    """{uid: {flags}} from a ``UID FETCH ... (FLAGS)`` reply."""
    out: dict[int, set[str]] = {}
    for raw in data or []:
        line = raw[0] if isinstance(raw, tuple) else raw
        if line is None:
            continue
        text = line if isinstance(line, bytes) else str(line).encode()
        uid_match = re.search(rb"UID\s+(\d+)", text)
        flag_match = re.search(rb"FLAGS\s+\(([^)]*)\)", text)
        if not uid_match:
            continue
        flags = set()
        if flag_match:
            flags = {f.decode(errors="replace") for f in flag_match.group(1).split()}
        out[int(uid_match.group(1))] = flags
    return out


def append_to_folder(conn, remote_folder: str, raw: bytes, *, flags: str = "\\Seen") -> int | None:
    """Put a copy of a sent message into the server's own folder.

    Without this the person's real mail client shows an empty "Отправленные":
    everything this system sends exists only in our database, which is exactly
    the divergence Ф2 is about. Returns the new UID when the server reports one
    (APPENDUID), else None — not every server does, and a missing UID is not a
    failure.
    """
    import imaplib
    import re
    import time

    stamp = imaplib.Time2Internaldate(time.time())
    status, data = conn.append(f'"{remote_folder}"', flags, stamp, raw)
    if status != "OK":
        raise RuntimeError(f"APPEND to {remote_folder} failed: {data!r}")

    for entry in data or []:
        line = entry if isinstance(entry, bytes) else str(entry).encode()
        match = re.search(rb"APPENDUID\s+\d+\s+(\d+)", line)
        if match:
            return int(match.group(1))
    return None
