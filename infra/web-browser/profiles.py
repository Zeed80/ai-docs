"""W2: kept browser profiles — a returning visitor instead of a stranger.

A profile is a Chrome ``user_data_dir`` on the ``browser_profiles`` volume:
cookies (a passed bot check, a consent banner, a login), local storage,
cache and history survive the request and the restart. Without it every
page load was a first visit from a fresh browser — the strongest bot signal
there is besides the headless flag.

A profile belongs to one person (``owner``, the user the work is done for)
and has a name: ``reader`` for reading pages, ``login`` for sessions that
sign in. The directory is named by a hash of the owner, so another owner's
profile is never reachable by name. Each open profile is its own Chrome
process with a fixed exit (direct, or one upstream proxy) and a fixed
persona (language, timezone), so IP, clock and language agree.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import egress

ROOT = Path(os.environ.get("BROWSER_PROFILES_DIR", "/profiles"))
MAX_OPEN = int(os.environ.get("BROWSER_MAX_OPEN_PROFILES", "4"))
# An open profile with no page in use is closed after this long.
IDLE_CLOSE_SECONDS = int(os.environ.get("BROWSER_PROFILE_IDLE_SECONDS", "300"))
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

DEFAULT_LOCALE = "ru-RU"
DEFAULT_TIMEZONE = "Europe/Moscow"


class ProfileError(Exception):
    """A refused profile operation; ``code`` is the answer's error."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Exit:
    """How a profile's traffic leaves: direct (None upstream) or via a proxy."""

    proxy_id: str | None = None
    upstream: egress.Upstream | None = None

    @property
    def key(self) -> str:
        if self.upstream is None:
            return "direct"
        u = self.upstream
        secret = hashlib.sha256(f"{u.username}:{u.password}".encode()).hexdigest()[:12]
        return f"{self.proxy_id}|{u.host}:{u.port}|{secret}"


@dataclass(frozen=True)
class Persona:
    locale: str = DEFAULT_LOCALE
    timezone: str = DEFAULT_TIMEZONE


@dataclass
class _Open:
    context: object
    exit_key: str
    persona: Persona
    in_use: int = 0
    last_used: float = field(default_factory=time.monotonic)


def owner_dir(owner: str) -> Path:
    return ROOT / hashlib.sha256(owner.encode()).hexdigest()[:24]


def check_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ProfileError("bad_profile_name")
    return name


class ProfileStore:
    def __init__(self, launch):
        # launch(user_data_dir, proxy_server, persona) -> persistent context
        self._launch = launch
        self._open: dict[tuple[str, str], _Open] = {}
        self._listeners: dict[str, tuple[object, int]] = {}
        self._lock = asyncio.Lock()

    async def _proxy_server(self, exit_: Exit, direct_port: int) -> str:
        if exit_.upstream is None:
            return f"http://127.0.0.1:{direct_port}"
        found = self._listeners.get(exit_.key)
        if found is None:
            # Loopback only: this listener carries one operator's proxy credentials.
            server, port = await egress.start_proxy("127.0.0.1", 0, exit_.upstream)
            found = self._listeners[exit_.key] = (server, port)
        return f"http://127.0.0.1:{found[1]}"

    def _alive(self, opened: _Open) -> bool:
        try:
            browser = opened.context.browser
            return browser is None or browser.is_connected()
        except Exception:  # noqa: BLE001
            return False

    async def acquire(self, owner: str, name: str, exit_: Exit, persona: Persona, direct_port: int):
        """The open context of this profile, launching it if needed; counted in use."""
        key = (owner, check_name(name))
        async with self._lock:
            opened = self._open.get(key)
            if opened is not None and (
                not self._alive(opened)
                or (
                    opened.in_use == 0
                    and (opened.exit_key != exit_.key or opened.persona != persona)
                )
            ):
                # Dead, or idle with another exit or persona: relaunch with the new one.
                await self._close(key)
                opened = None
            if opened is None:
                await self._make_room()
                directory = owner_dir(owner) / name
                directory.mkdir(parents=True, exist_ok=True)
                (owner_dir(owner) / "owner.json").write_text(json.dumps({"owner": owner}))
                proxy = await self._proxy_server(exit_, direct_port)
                context = await self._launch(str(directory / "chrome"), proxy, persona)
                opened = self._open[key] = _Open(context, exit_.key, persona)
                self._touch_meta(owner, name, exit_)
            opened.in_use += 1
            opened.last_used = time.monotonic()
            return opened.context

    def release(self, owner: str, name: str) -> None:
        opened = self._open.get((owner, name))
        if opened is not None:
            opened.in_use = max(0, opened.in_use - 1)
            opened.last_used = time.monotonic()

    def is_open(self, owner: str, name: str, context) -> bool:
        opened = self._open.get((owner, name))
        return opened is not None and opened.context is context and self._alive(opened)

    async def _make_room(self) -> None:
        if len(self._open) < MAX_OPEN:
            return
        idle = sorted((o.last_used, key) for key, o in self._open.items() if o.in_use == 0)
        if not idle:
            raise ProfileError("profile_limit")
        await self._close(idle[0][1])

    async def _close(self, key: tuple[str, str]) -> None:
        opened = self._open.pop(key, None)
        if opened is None:
            return
        try:
            await asyncio.wait_for(opened.context.close(), 10)
        except Exception:  # noqa: BLE001 — already gone
            pass

    async def sweep(self) -> int:
        now = time.monotonic()
        async with self._lock:
            stale = [
                key
                for key, o in self._open.items()
                if (o.in_use == 0 and now - o.last_used > IDLE_CLOSE_SECONDS) or not self._alive(o)
            ]
            for key in stale:
                await self._close(key)
        return len(stale)

    def _touch_meta(self, owner: str, name: str, exit_: Exit) -> None:
        meta_path = owner_dir(owner) / name / "meta.json"
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:  # noqa: BLE001
            meta = {"created": time.time()}
        meta.update(last_used=time.time(), proxy_id=exit_.proxy_id)
        meta_path.write_text(json.dumps(meta))

    def list(self, owner: str) -> list[dict]:
        base = owner_dir(owner)
        out = []
        if not base.is_dir():
            return out
        for directory in sorted(p for p in base.iterdir() if p.is_dir()):
            try:
                meta = json.loads((directory / "meta.json").read_text())
            except Exception:  # noqa: BLE001
                meta = {}
            size = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
            opened = self._open.get((owner, directory.name))
            out.append(
                {
                    "name": directory.name,
                    "size_bytes": size,
                    "created": meta.get("created"),
                    "last_used": meta.get("last_used"),
                    "proxy_id": meta.get("proxy_id"),
                    "open": opened is not None,
                    "in_use": bool(opened and opened.in_use),
                }
            )
        return out

    async def delete(self, owner: str, name: str) -> bool:
        key = (owner, check_name(name))
        async with self._lock:
            opened = self._open.get(key)
            if opened is not None and opened.in_use:
                raise ProfileError("profile_in_use")
            await self._close(key)
            directory = owner_dir(owner) / name
            if not directory.is_dir():
                return False
            shutil.rmtree(directory, ignore_errors=True)
            return True

    async def close_all(self) -> None:
        for key in list(self._open):
            await self._close(key)
        for server, _port in self._listeners.values():
            server.close()
        self._listeners.clear()
