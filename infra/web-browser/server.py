"""Human-like page fetcher.

A FastAPI service that drives a real Google Chrome (``patchright``) in a
window on a virtual display, so pages load the way a person's browser would:
JavaScript executes, ``navigator.webdriver`` is hidden, and the fingerprint
is the engine's own — no hand-written user agent or client hints that
contradict it (W2: those, the headless mode and a fresh profile per request
were what gave the old browser away). Profiles are kept (:mod:`profiles`),
input moves and types like a person (:mod:`humanize`), and a JS bot check is
given time to clear before the wall is reported (:mod:`challenges`). It
returns the rendered page's readable text and, on request, a screenshot.

This service is the ONLY component with outbound internet access for browsing;
the backend proxies to it over the internal Docker network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

import challenges
import egress
import humanize
import profiles
from patchright.async_api import async_playwright
from pydantic import BaseModel, Field
from urllib.parse import urlparse

try:
    import trafilatura
except Exception:  # noqa: BLE001
    trafilatura = None

try:
    import fitz  # PyMuPDF — extract text from PDF catalogs/datasheets.
except Exception:  # noqa: BLE001
    fitz = None

# W2: Google Chrome stable, in a window. "chromium" (the bundled build) is the
# fallback when Chrome is not installed; BROWSER_HEADLESS=1 only for tests
# that have no display.
CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome")
HEADLESS = os.environ.get("BROWSER_HEADLESS", "0") == "1"
_WINDOW = (1366, 900)
_DISPLAY_SIZE = "1920x1080x24"

# No user agent, client hints or Accept-Language of our own: Chrome sends its
# real ones, which agree with its engine, platform and fonts. WebGL must
# exist (none at all is the stronger bot signal): with the GPU blocklist
# ignored Chrome renders through Mesa llvmpipe — what a Linux desktop without
# a GPU driver reports — instead of the "SwiftShader" renderer known from VMs.
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--ignore-gpu-blocklist",
    "--enable-unsafe-swiftshader",
    f"--window-size={_WINDOW[0]},{_WINDOW[1]}",
]

_display_lock = asyncio.Lock()


async def _ensure_display() -> None:
    """A virtual screen for the windowed browser, started once per process."""
    global HEADLESS
    if HEADLESS or os.environ.get("DISPLAY"):
        return
    async with _display_lock:
        if os.environ.get("DISPLAY"):
            return
        number = 99
        try:
            await asyncio.create_subprocess_exec(
                "Xvfb", f":{number}", "-screen", "0", _DISPLAY_SIZE, "-nolisten", "tcp",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            HEADLESS = True  # no Xvfb: degrade, do not fail every page
            return
        socket_path = f"/tmp/.X11-unix/X{number}"
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            await asyncio.sleep(0.1)
        os.environ["DISPLAY"] = f":{number}"


def _persona(value) -> profiles.Persona:
    if value is None:
        return profiles.Persona()
    return profiles.Persona(
        locale=value.locale or profiles.DEFAULT_LOCALE,
        timezone=value.timezone or profiles.DEFAULT_TIMEZONE,
    )


def _exit(value) -> profiles.Exit:
    if value is None:
        return profiles.Exit()
    return profiles.Exit(
        proxy_id=value.id,
        upstream=egress.Upstream.parse(value.server, value.username or "", value.password or ""),
    )


class _Browser:
    """Lazily-started shared Chromium; one fresh context per fetch."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._proxy = None
        self.proxy_port: int | None = None
        self._lock = asyncio.Lock()

    async def driver(self):
        if self._pw is None:
            await _ensure_display()
            self._pw = await async_playwright().start()
        if self._proxy is None:
            # E33: every request leaves through the egress proxy.
            await self.start_proxy()
        return self._pw

    async def ensure(self):
        async with self._lock:
            if self._browser is None or not self.alive():
                # E37: after a crash of Chromium or of the driver, start over:
                # a dead driver cannot launch anything.
                await self._reset_dead()
                await self.driver()
                self._browser = await self._pw.chromium.launch(
                    channel=CHANNEL,
                    headless=HEADLESS,
                    args=[*_LAUNCH_ARGS, "--proxy-bypass-list=<-loopback>"],
                    proxy={"server": f"http://127.0.0.1:{self.proxy_port}"},
                )
            return self._browser

    async def launch_profile(self, user_data_dir: str, proxy_server: str, persona):
        """One kept profile = one Chrome process with its own exit and persona."""
        pw = await self.driver()
        return await pw.chromium.launch_persistent_context(
            user_data_dir,
            channel=CHANNEL,
            headless=HEADLESS,
            no_viewport=True,
            args=[*_LAUNCH_ARGS, f"--lang={persona.locale}", "--proxy-bypass-list=<-loopback>"],
            proxy={"server": proxy_server},
            locale=persona.locale,
            timezone_id=persona.timezone,
            env={**os.environ, "TZ": persona.timezone},
            service_workers="block",
            ignore_https_errors=True,
            accept_downloads=True,
        )

    async def _reset_dead(self) -> None:
        for closer in (
            (self._browser.close if self._browser is not None else None),
            (self._pw.stop if self._pw is not None else None),
        ):
            if closer is None:
                continue
            try:
                await asyncio.wait_for(closer(), 5)
            except Exception:  # noqa: BLE001 — it is already dead
                pass
        self._browser = None
        self._pw = None
        self._dead = False

    def mark_dead(self) -> None:
        """The driver or browser is gone even if the object has not noticed."""
        self._dead = True

    def alive(self) -> bool:
        if getattr(self, "_dead", False):
            return False
        try:
            return self._browser is not None and self._browser.is_connected()
        except Exception:  # noqa: BLE001
            return False

    async def start_proxy(self) -> None:
        if self._proxy is None:
            self._proxy, self.proxy_port = await egress.start_proxy()

    async def close(self) -> None:
        await self._reset_dead()
        if self._proxy is not None:
            self._proxy.close()
            self._proxy = None


_engine = _Browser()
_profiles = profiles.ProfileStore(_engine.launch_profile)
_sessions: dict[str, dict] = {}

# E31: a desktop session belongs to the work that started it. Every action
# names its owner; a wrong owner reads exactly like a missing session, so a
# known session id gives nothing. Sessions end on their own.
# W2: a working session lasts like a person's — up to two hours while used.
SESSION_IDLE_SECONDS = int(os.environ.get("BROWSER_SESSION_IDLE_SECONDS", "900"))
SESSION_MAX_SECONDS = int(os.environ.get("BROWSER_SESSION_MAX_SECONDS", "7200"))
MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "8"))
MAX_SESSIONS_PER_OWNER = int(os.environ.get("BROWSER_MAX_SESSIONS_PER_OWNER", "2"))
MAX_TABS = int(os.environ.get("BROWSER_MAX_TABS", "8"))
# Child frames an observe looks into (each is listed with its own refs).
MAX_FRAMES = int(os.environ.get("BROWSER_MAX_FRAMES", "6"))
# E35: downloads are read into memory under a size and time limit, at most a
# few per session; the backend registers them through its artifact broker.
DOWNLOAD_MAX_BYTES = int(os.environ.get("BROWSER_DOWNLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
DOWNLOAD_MAX_SECONDS = int(os.environ.get("BROWSER_DOWNLOAD_MAX_SECONDS", "180"))
MAX_DOWNLOADS = int(os.environ.get("BROWSER_MAX_DOWNLOADS", "20"))
_SIGNATURES = (
    (b"%PDF", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"\x7fELF", "application/x-executable"),
    (b"MZ", "application/x-msdownload"),
)


def _sniff(data: bytes) -> str:
    for magic, mime in _SIGNATURES:
        if data.startswith(magic):
            return mime
    try:
        data[:4096].decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _display_name(suggested: str) -> str:
    """A flat label from Content-Disposition: never a path, never trusted."""
    base = (suggested or "download").replace("\\", "/").rsplit("/", 1)[-1]
    clean = "".join(c if (c.isascii() and c.isalnum()) or c in "._-" else "_" for c in base)
    while ".." in clean:
        clean = clean.replace("..", "_")
    clean = clean.lstrip(".-")  # no hidden or option-like names; "_" may lead
    return (clean or "download")[:100]


async def _capture_download(session: dict, download) -> None:
    downloads = session.setdefault("downloads", {})
    if len(downloads) >= MAX_DOWNLOADS:
        await download.cancel()
        return
    record = {
        "id": uuid.uuid4().hex[:16],
        "url": download.url,
        "name": _display_name(download.suggested_filename),
        "status": "running",
    }
    downloads[record["id"]] = record
    try:
        # The egress proxy cuts any response over its byte limit, so an endless
        # body cannot fill the disk before this times out.
        path = await asyncio.wait_for(download.path(), DOWNLOAD_MAX_SECONDS)
    except (TimeoutError, asyncio.TimeoutError):
        record.update(status="refused", reason="too_slow")
        await download.cancel()
        return
    except Exception as exc:  # noqa: BLE001 — canceled, cut or the page closed
        record.update(status="failed", reason=str(exc)[:200])
        return
    failure = await download.failure()
    if failure or path is None:
        record.update(status="failed", reason=str(failure or "no_file")[:200])
        return
    try:
        size = os.path.getsize(path)
        if size > DOWNLOAD_MAX_BYTES:
            record.update(status="refused", reason="too_large", size=size)
            return
        with open(path, "rb") as handle:
            data = handle.read()
    finally:
        await download.delete()
    record.update(
        status="done",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        sniffed_type=_sniff(data),
        data=data,
    )


def _watch_downloads(session: dict, page) -> None:
    page.on("download", lambda d: asyncio.create_task(_capture_download(session, d)))


def _session_pages(session: dict) -> list:
    """This session's own tabs. A kept profile is shared by the owner's
    sessions, so the context's page list is not the session's."""
    session["pages"] = [p for p in session["pages"] if not p.is_closed()]
    return session["pages"]


def _adopt_page(session: dict, page) -> None:
    """A tab the session opened (link with target=_blank, window.open)."""
    if len(_session_pages(session)) >= MAX_TABS:
        asyncio.create_task(page.close())
        return
    session["pages"].append(page)
    _watch_downloads(session, page)
    page.on("popup", lambda popup: _adopt_page(session, popup))
    page.on("request", lambda request: _note_write(session, request))


# W3: what left the page. A form that navigated with POST, or any request
# whose body carries text the agent typed in this session, is recorded and
# reported with the action that caused it — a "read" click on a script
# button that sent data is not hidden behind its class.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TYPED_MIN = 3


def _note_write(session: dict, request) -> None:
    try:
        if request.method not in _WRITE_METHODS:
            return
        body = request.post_data or ""
    except Exception:  # noqa: BLE001 — a binary body cannot be decoded
        body = ""
    from urllib.parse import unquote_plus

    decoded = unquote_plus(body)
    typed = [t for t in session.get("typed", ()) if len(t) >= _TYPED_MIN]
    carries = any(t in body or t in decoded for t in typed)
    if not carries and request.resource_type != "document":
        return
    parts = urlparse(request.url)
    session.setdefault("writes", []).append(
        {
            "method": request.method,
            "url": f"{parts.scheme}://{parts.netloc}{parts.path}"[:300],
            "form_navigation": request.resource_type == "document",
            "carries_typed_text": carries,
            "at": time.monotonic(),
        }
    )
    del session["writes"][:-50]


def _session_alive(session: dict) -> bool:
    key = session.get("profile_key")
    if key is not None:
        return _profiles.is_open(*key, session["context"])
    return session["browser"] is _engine._browser and _engine.alive()


def _expired(session: dict, now: float) -> bool:
    return (
        now - session["last_used"] > SESSION_IDLE_SECONDS
        or now - session["created"] > SESSION_MAX_SECONDS
    )


async def _close_session(session_id: str) -> bool:
    session = _sessions.pop(session_id, None)
    if session is None:
        return False
    key = session.get("profile_key")
    try:
        if key is None:
            await session["context"].close()
        else:
            # The profile stays open for the owner's other work; only this
            # session's tabs go. Its cookies are kept in the profile.
            for page in list(session["pages"]):
                if not page.is_closed():
                    await page.close()
    except Exception:  # noqa: BLE001 — the context may already be gone
        pass
    finally:
        if key is not None:
            _profiles.release(*key)
    return True


def _drop_sessions_like(session: dict) -> None:
    """E37: the browser (or the profile's Chrome) died under these sessions."""
    for sid, other in list(_sessions.items()):
        same = (
            other.get("profile_key") == session.get("profile_key")
            and other["context"] is session["context"]
            if session.get("profile_key") is not None
            else other.get("profile_key") is None and other["browser"] is session["browser"]
        )
        if same:
            _sessions.pop(sid, None)
            if other.get("profile_key") is not None:
                _profiles.release(*other["profile_key"])


async def _sweep_expired() -> int:
    now = time.monotonic()
    expired = [sid for sid, session in list(_sessions.items()) if _expired(session, now)]
    for sid in expired:
        await _close_session(sid)
    return len(expired)


async def _sweeper() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await _sweep_expired()
            await _profiles.sweep()
        except Exception:  # noqa: BLE001 — keep sweeping
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The proxy serves the backend's workers too, before any browsing.
    await _engine.start_proxy()
    sweeper = asyncio.create_task(_sweeper())
    yield
    sweeper.cancel()
    for sid in list(_sessions):
        await _close_session(sid)
    await _profiles.close_all()
    await _engine.close()


app = FastAPI(title="web-browser", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_without_input(_request, exc: RequestValidationError):
    """E34: a validation error must not echo the request — it may carry a secret."""
    return JSONResponse(
        status_code=422,
        content={"detail": [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]},
    )


class ProxyIn(BaseModel):
    """An upstream proxy from the operator's pool; the backend picks it."""

    id: str = Field(min_length=1, max_length=40)
    server: str = Field(min_length=4, max_length=300)
    username: str | None = Field(default=None, max_length=300)
    password: str | None = Field(default=None, max_length=300, repr=False)


class PersonaIn(BaseModel):
    locale: str | None = Field(default=None, max_length=20)
    timezone: str | None = Field(default=None, max_length=60)


class ProfileFields(BaseModel):
    # W2: whose kept profile to use. None → a fresh incognito context.
    profile_owner: str | None = Field(default=None, max_length=200)
    profile: str = Field("reader", max_length=40)
    proxy: ProxyIn | None = None
    persona: PersonaIn | None = None


class FetchRequest(ProfileFields):
    url: str = Field(..., min_length=4, max_length=2048)
    screenshot: bool = False
    max_chars: int = Field(20000, ge=500, le=200000)
    wait_ms: int = Field(0, ge=0, le=15000)
    # For a scanned PDF (no text layer): how many pages to rasterize for OCR.
    pdf_ocr_pages: int = Field(5, ge=0, le=20)
    # Also return the page's outgoing links (absolute URL + anchor text). Used
    # to DISCOVER a supplier's catalogs/price lists: the extracted text says
    # nothing about which file a "Скачать каталог" button points at.
    include_links: bool = False
    max_links: int = Field(200, ge=1, le=1000)


class PageLink(BaseModel):
    url: str
    text: str = ""


class FetchResponse(BaseModel):
    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    text: str = ""
    links: list[PageLink] = []
    screenshot_b64: str | None = None
    truncated: bool = False
    # Base64 PNGs of scanned-PDF pages, for the backend to OCR.
    page_images_b64: list[str] = []
    diagnostics: list[str] = []


class DesktopStartRequest(ProfileFields):
    url: str = Field(..., min_length=4, max_length=2048)
    allowed_hosts: list[str] = Field(min_length=1, max_length=50)
    owner: str = Field(min_length=1, max_length=200)


class DesktopCloseOwnerRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=200)


class DesktopActionRequest(BaseModel):
    session_id: str
    owner: str = Field(min_length=1, max_length=200)
    action: str = Field(
        pattern="^(observe|describe|click|type|fill_secret|upload|read|screenshot|navigate|tabs|switch_tab|downloads|take_download|close|handoff|scroll|press|hover|select|wait_for|back|forward|open|search)$"
    )
    # E32: click/type address an element by the ref an observe returned,
    # under that observe's revision — never a model-written CSS selector
    # or a coordinate.
    ref: str | None = Field(default=None, max_length=40)
    revision: int | None = Field(default=None, ge=1)
    text: str | None = Field(default=None, max_length=200000)
    url: str | None = Field(default=None, max_length=2048)
    tab: int | None = Field(default=None, ge=0, le=20)
    wait_ms: int = Field(0, ge=0, le=15000)
    # E34: only for fill_secret, from the backend's broker; never echoed.
    secret: str | None = Field(default=None, max_length=4000, repr=False)
    origin: str | None = Field(default=None, max_length=500)
    # E35: upload — in-memory files from the backend's artifact broker; no
    # host path ever reaches the browser. take_download — this session's id.
    files: list[dict] | None = Field(default=None, max_length=5)
    # E36: the action card an approval was given for; a click under it runs
    # only if the live form still describes to the same card.
    card: dict | None = None
    download_id: str | None = Field(default=None, max_length=40)
    # T3: why the agent hands the session to its owner (MFA, CAPTCHA).
    reason: str | None = Field(default=None, max_length=500)
    # W2: scroll — pixels down (negative: up); press — one key of _AGENT_KEYS.
    dy: int | None = Field(default=None, ge=-20000, le=20000)
    key: str | None = Field(default=None, max_length=20)
    # W3: search — press Enter in the field after typing.
    submit: bool = False
    # W3: hosts of the grant this action runs under; the page must be in them.
    grant_hosts: list[str] | None = Field(default=None, max_length=50)


class DesktopHumanRequest(BaseModel):
    """T3: the session's owner, a person, looks at it and acts in it."""

    session_id: str
    owner: str = Field(min_length=1, max_length=200)
    op: str = Field(pattern="^(view|take|click|type|key|return)$")
    x: int | None = Field(default=None, ge=0, le=10000)
    y: int | None = Field(default=None, ge=0, le=10000)
    text: str | None = Field(default=None, max_length=500, repr=False)
    key: str | None = Field(default=None, max_length=20)


class DesktopListOwnerRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=200)


# T3: keys a person may press in a handed-off session.
_HUMAN_KEYS = frozenset(
    {"Enter", "Tab", "Shift+Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown",
     "ArrowLeft", "ArrowRight", "Space"}
)
# W2: keys the agent may press — navigation and form keys, no shortcuts that
# reach the browser itself (address bar, devtools, downloads).
_AGENT_KEYS = frozenset(
    {"Enter", "Tab", "Shift+Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown",
     "ArrowLeft", "ArrowRight", "Space", "PageUp", "PageDown", "Home", "End"}
)
# What a person types is redacted from the agent's answers like a secret
# (an MFA code must not reach the model); shorter text would blank out
# ordinary page characters, so it is not tracked.
_HUMAN_TEXT_REDACT_MIN = 4


# E32: what an observe sees. Interactive elements of the active tab's main
# frame get a ref "<revision>:<n>" written into the DOM; a MutationObserver
# marks the page dirty on any structural change after that, so an action under
# an old revision is refused instead of hitting whatever is there now.
# W2: elements of child frames get "<revision>:<n>@<frame>" and are
# addressable too (an embedded catalog, a search widget); each frame keeps its
# own revision mark. The iframe element itself is still not actionable.
# E36: what a click would send: the element and its form, values normalized.
_DESCRIBE_JS = """
(el) => {
  const form = el.form || el.closest('form');
  const fields = form ? Array.from(form.elements).filter(f => f.name).map(f => {
    const type = (f.type || f.tagName).toLowerCase();
    let value;
    if (type === 'password') value = f.value ? '(set)' : '(empty)';
    else if (type === 'checkbox' || type === 'radio') value = f.checked ? (f.value || 'on') : null;
    else if (type === 'file') value = Array.from(f.files || []).map(x => x.name + ':' + x.size);
    else value = f.value === undefined ? null : String(f.value);
    return {name: f.name, type, value};
  }) : [];
  return {
    element: {tag: el.tagName.toLowerCase(), type: el.type || null,
      name: (el.getAttribute('aria-label') || (el.innerText || '').trim() || el.value || '')
        .toString().trim().slice(0, 200)},
    form: form ? {action: form.action || null, method: (form.method || 'get').toLowerCase(),
      fields} : null,
  };
}
"""


def _card_hash(card: dict) -> str:
    import json as _json

    body = {k: v for k, v in card.items() if k != "card_hash"}
    return hashlib.sha256(
        _json.dumps(body, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


# W3: what acting on an element would do, from facts of the DOM only — no
# words of the page are read. read: a link, a tab, a toggle, a button outside
# any data form, a search or filter field, a GET form of such fields.
# submit: a form that sends data (POST, or e-mail/phone/name/address/text
# fields); login: a form with a password; transact: payment card fields
# (autocomplete="cc-*"). Inside a form the form decides, whatever the button's
# type: a type=button that submits by script is still that form's submit.
_CLASSIFY_JS = """
(el) => {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const role = (el.getAttribute('role') || '').toLowerCase();
  const ac = f => (f.getAttribute && f.getAttribute('autocomplete') || '').toLowerCase();
  const PERSONAL = /^(name|given-name|family-name|additional-name|email|tel|street-address|address-line|address-level|postal-code|country-name|organization|organization-title|username|new-password|current-password|bday|sex|cc-)/;
  const personalField = f => {
    const t = (f.type || '').toLowerCase();
    return ['email', 'tel', 'password', 'file'].includes(t) || f.tagName === 'TEXTAREA'
      || f.isContentEditable || PERSONAL.test(ac(f));
  };
  const form = el.form || el.closest('form');
  let formClass = null, method = null;
  if (form) {
    const fields = Array.from(form.elements);
    method = (form.getAttribute('method') || 'get').toLowerCase();
    if (fields.some(f => ac(f).startsWith('cc-'))) formClass = 'transact';
    else if (fields.some(f => (f.type || '').toLowerCase() === 'password')) formClass = 'login';
    else if (method !== 'get' || fields.some(personalField)) formClass = 'submit';
    else formClass = 'read';
  }
  let kind = 'other', cls = formClass || 'read';
  if (tag === 'a' && el.getAttribute('href') && !/^javascript:/i.test(el.getAttribute('href'))) {
    kind = 'link';
    const m = (el.getAttribute('data-method') || '').toLowerCase();
    cls = m && m !== 'get' ? 'submit' : 'read';
  } else if (tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable) {
    kind = 'field';
    if (type === 'password') cls = 'login';
    else if (ac(el).startsWith('cc-')) cls = 'transact';
    else if (personalField(el)) cls = formClass && formClass !== 'read' ? formClass : 'submit';
    if (['submit', 'image'].includes(type)) kind = 'button';
  } else if (tag === 'button' || role === 'button') {
    kind = 'button';
  } else if (['tab', 'menuitem', 'option', 'treeitem', 'switch'].includes(role)
             || tag === 'summary' || el.hasAttribute('aria-expanded')) {
    kind = 'toggle';
    cls = formClass === 'transact' ? 'transact' : 'read';
  }
  return {class: cls, kind, form_method: method};
}
"""

# Order of the classes by what they may cost: a mismatch is refused upward.
_CLASS_RANK = {"read": 0, "submit": 1, "login": 2, "transact": 3}


async def _classify(target) -> dict:
    return await target.evaluate(_CLASSIFY_JS)


async def _describe(page, target) -> dict:
    described = await target.evaluate(_DESCRIBE_JS)
    facts = await _classify(target)
    card = {
        "origin": _page_origin(page.url),
        "url": page.url,
        "class": facts["class"],
        **described,
    }
    card["card_hash"] = _card_hash(card)
    return card


_OBSERVE_JS = """
(arg) => {
  const rev = arg.rev;
  const tag = arg.frame ? '@' + arg.frame : '';
  // State lives in the DOM: page.evaluate may run in an isolated world that
  // does not keep window properties between calls.
  if (window.__aiwObserver) window.__aiwObserver.disconnect();
  document.querySelectorAll('[data-aiw-ref]').forEach(e => e.removeAttribute('data-aiw-ref'));
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],' +
    '[role=checkbox],[role=tab],[role=menuitem],[contenteditable=true],iframe';
  const visible = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
    return st.visibility !== 'hidden' && st.display !== 'none' && (r.width > 0 || r.height > 0); };
  const roles = {A: 'link', BUTTON: 'button', SELECT: 'combobox', TEXTAREA: 'textbox', IFRAME: 'frame'};
  const els = Array.from(document.querySelectorAll(sel)).filter(visible).slice(0, 300);
  const out = els.map((e, i) => {
    const ref = rev + ':' + (i + 1) + tag;
    e.setAttribute('data-aiw-ref', ref);
    let role = e.getAttribute('role') || roles[e.tagName];
    if (!role && e.tagName === 'INPUT') {
      role = ['checkbox', 'radio'].includes(e.type) ? e.type
        : ['submit', 'button', 'reset'].includes(e.type) ? 'button' : 'textbox';
    }
    const name = (e.getAttribute('aria-label') || (e.innerText || '').trim() || e.value ||
      e.getAttribute('placeholder') || e.getAttribute('title') || e.getAttribute('name') || '')
      .toString().trim().slice(0, 200);
    return {ref, role: role || e.tagName.toLowerCase(), name, tag: e.tagName.toLowerCase(),
      disabled: !!(e.disabled || e.getAttribute('aria-disabled') === 'true'),
      type: e.type || null,
      value: (e.tagName === 'INPUT' && e.type === 'password') ? null
        : (e.value !== undefined && e.tagName !== 'BUTTON' ? String(e.value).slice(0, 200) : null),
      href: e.href || null};
  });
  const root = document.documentElement;
  root.setAttribute('data-aiw-rev', String(rev));
  const obs = new MutationObserver(muts => {
    const onlyStyle = m => m.type === 'childList' &&
      [...m.addedNodes, ...m.removedNodes].every(n => n.nodeName === 'STYLE');
    for (const m of muts) {
      if (m.type === 'attributes' && (m.attributeName || '').startsWith('data-aiw-')) continue;
      // The screenshot's caret/animation style is injected by the browser
      // driver, not a change of the page the agent saw.
      if (onlyStyle(m)) continue;
      root.removeAttribute('data-aiw-rev'); return;
    }
  });
  obs.observe(document.documentElement,
    {subtree: true, childList: true, attributes: true, characterData: true});
  window.__aiwObserver = obs;
  return {elements: out, text: document.body ? document.body.innerText.slice(0, 20000) : ''};
}
"""


def _session_host_allowed(url: str, hosts: list[str]) -> bool:
    """W3: "*" — any public site (the read grant); egress still refuses
    private and internal addresses, whatever the allowlist says."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and any(
        allowed == "*" or host == allowed.casefold() or host.endswith("." + allowed.casefold())
        for allowed in hosts
    )


async def _new_browser_context(req: ProfileFields | None = None):
    """A fresh incognito context: no cookies, storage or cache of anyone."""
    browser = await _engine.ensure()
    persona = _persona(req.persona if req else None)
    options = {}
    if req is not None and req.proxy is not None:
        proxy_server = await _profiles._proxy_server(_exit(req.proxy), _engine.proxy_port)
        options["proxy"] = {"server": proxy_server}
    return await browser.new_context(
        # E33: a service worker would fetch outside page routing and outlive it.
        service_workers="block",
        no_viewport=True,
        locale=persona.locale,
        timezone_id=persona.timezone,
        java_script_enabled=True,
        ignore_https_errors=True,
        **options,
    )


async def _context_for(req: ProfileFields):
    """(context, profile key or None): the owner's kept profile or a fresh one."""
    if not req.profile_owner:
        return await _new_browser_context(req), None
    await _engine.driver()
    context = await _profiles.acquire(
        req.profile_owner, req.profile, _exit(req.proxy), _persona(req.persona), _engine.proxy_port
    )
    return context, (req.profile_owner, req.profile)


async def _viewport(page) -> dict:
    width, height = await page.evaluate("() => [innerWidth, innerHeight]")
    return {"width": int(width), "height": int(height)}


def _extract_pdf_text(data: bytes, max_chars: int) -> tuple[str, str | None]:
    """Extract text (and title) from PDF bytes. Returns ("", None) if unavailable."""
    if fitz is None:
        return "", None
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        return "", None
    parts: list[str] = []
    total = 0
    for page in doc:
        try:
            chunk = page.get_text("text") or ""
        except Exception:  # noqa: BLE001
            continue
        parts.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    title = None
    try:
        title = (doc.metadata or {}).get("title") or None
    except Exception:  # noqa: BLE001
        pass
    doc.close()
    return "\n".join(parts).strip(), title


def _render_pdf_images(data: bytes, max_pages: int) -> list[str]:
    """Rasterize the first pages of a (scanned) PDF to base64 PNGs for OCR."""
    if fitz is None or max_pages <= 0:
        return []
    out: list[str] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        return []
    # 200 DPI keeps text legible for the OCR model without huge payloads.
    matrix = fitz.Matrix(200 / 72, 200 / 72)
    for page in doc:
        if len(out) >= max_pages:
            break
        try:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
        except Exception:  # noqa: BLE001
            continue
    doc.close()
    return out


def _extract_text(html: str, url: str) -> str:
    if trafilatura is not None:
        try:
            extracted = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                favor_recall=True,
            )
            if extracted:
                return extracted
        except Exception:  # noqa: BLE001
            pass
    return ""


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/desktop/start")
async def desktop_start(req: DesktopStartRequest) -> dict:
    if not _session_host_allowed(req.url, req.allowed_hosts):
        return {"ok": False, "error": "host_not_allowed"}
    await _sweep_expired()
    if len(_sessions) >= MAX_SESSIONS:
        return {"ok": False, "error": "session_limit"}
    if sum(1 for s in _sessions.values() if s["owner"] == req.owner) >= MAX_SESSIONS_PER_OWNER:
        return {"ok": False, "error": "owner_session_limit"}
    try:
        context, profile_key = await _context_for(req)
    except profiles.ProfileError as exc:
        return {"ok": False, "error": exc.code}
    except ValueError as exc:  # a malformed upstream proxy
        return {"ok": False, "error": f"bad_proxy:{str(exc)[:100]}"}
    session_id = str(uuid.uuid4())
    now = time.monotonic()
    session = {
        "context": context,
        "pages": [],
        "profile_key": profile_key,
        "allowed_hosts": list(req.allowed_hosts),
        "lock": asyncio.Lock(),
        "owner": req.owner,
        "created": now,
        "last_used": now,
        "browser": _engine._browser if profile_key is None else None,
    }
    page = await context.new_page()
    session["page"] = page
    _adopt_page(session, page)
    try:
        response = await page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        response = None
    if not _session_host_allowed(page.url, req.allowed_hosts):
        _sessions[session_id] = session
        await _close_session(session_id)
        return {"ok": False, "error": "navigation_left_allowlist"}
    _sessions[session_id] = session
    challenge, cleared = await challenges.await_clearance(page, session, response)
    shot = await page.screenshot(type="png", animations="disabled", caret="initial")
    answer = {
        "ok": True,
        "session_id": session_id,
        "url": page.url,
        "status": response.status if response else None,
        "title": await page.title(),
        "screenshot_b64": base64.b64encode(shot).decode("ascii"),
    }
    if challenge:
        answer["challenge"] = {**challenge, "cleared": cleared}
    return answer


@app.post("/desktop/action")
async def desktop_action(req: DesktopActionRequest) -> dict:
    session = _sessions.get(req.session_id)
    # Another owner's session answers exactly like a missing one.
    if session is None or session["owner"] != req.owner:
        return {"ok": False, "error": "session_not_found"}
    if not _session_alive(session):
        _drop_sessions_like(session)
        return {"ok": False, "error": "session_lost"}
    if _expired(session, time.monotonic()):
        await _close_session(req.session_id)
        return {"ok": False, "error": "session_expired"}
    async with session["lock"]:
        if _sessions.get(req.session_id) is not session:
            # Closed (by owner, revoke or sweep) while this action waited.
            return {"ok": False, "error": "session_not_found"}
        session["last_used"] = time.monotonic()
        if session.get("handoff") is not None:
            # T3: the owner holds the session; the agent waits for it back.
            return {"ok": False, "error": "session_with_owner"}
        if req.action == "close":
            await _close_session(req.session_id)
            return {"ok": True, "closed": True}
        if req.action == "handoff":
            session["handoff"] = {"reason": (req.reason or "")[:500], "since": time.time()}
            return {"ok": True, "handed_off": True, "url": session["page"].url}
        if req.grant_hosts is not None and not _session_host_allowed(
            session["page"].url, req.grant_hosts
        ):
            # W3: a grant for one portal does not act on a page of another
            # site this session reached under the read grant.
            return {"ok": False, "error": "page_outside_grant", "url": session["page"].url}
        started = time.monotonic()
        try:
            result = await _desktop_step(session, req)
        except Exception as exc:  # noqa: BLE001
            if _dead_browser_error(exc) and session.get("profile_key") is None:
                _engine.mark_dead()
            if not _session_alive(session):
                # E37: the browser (or its driver) died. Every session on it is
                # gone; nothing is retried here — the caller reconciles.
                _drop_sessions_like(session)
                return {"ok": False, "error": "session_lost"}
            error = f"action_failed:{str(exc)[:300]}"
            if req.secret:
                error = error.replace(req.secret, "•••")
            return _redact(session, {"ok": False, "error": error})
        if result.get("ok") is False:
            return result
        if req.action in {"open", "search", "press", "select", "click", "type"}:
            # Requests a page fires a moment after the action are its doing.
            await asyncio.sleep(0.3)
        sent = [
            {k: v for k, v in w.items() if k != "at"}
            for w in session.get("writes", [])
            if w["at"] >= started
        ]
        if sent:
            result["sent"] = sent
        page = session["page"]
        if not _session_host_allowed(page.url, session["allowed_hosts"]):
            await _close_session(req.session_id)
            return {"ok": False, "error": "navigation_left_allowlist"}
        shot = await page.screenshot(type="png", animations="disabled", caret="initial")
        return _redact(
            session,
            {
                "ok": True,
                "url": page.url,
                "title": await page.title(),
                "tab": _session_pages(session).index(page) if page in session["pages"] else 0,
                # E32: what the page says is data from the site, never an
                # instruction to the agent or a change of its owner or rights.
                "content_trust": "untrusted_page",
                **result,
                "screenshot_b64": base64.b64encode(shot).decode("ascii"),
            },
        )


def _redact(session: dict, value):
    """E34: a secret typed in this session never comes back in an answer,
    even if the page repeats it in a visible field, its text, URL or title."""
    secrets = session.get("secrets") or ()
    if not secrets:
        return value
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "•••")
        return value
    if isinstance(value, list):
        return [_redact(session, v) for v in value]
    if isinstance(value, dict):
        return {
            k: (v if k in {"screenshot_b64", "content_b64"} else _redact(session, v))
            for k, v in value.items()
        }
    return value


def _page_origin(url: str) -> str:
    parts = urlparse(url)
    scheme = parts.scheme
    default = {"http": 80, "https": 443}.get(scheme)
    port = parts.port or default
    host = (parts.hostname or "").lower()
    return f"{scheme}://{host}" + ("" if port == default else f":{port}")


_DEAD_MARKERS = (
    "connection closed",
    "browser has been closed",
    "browser has disconnected",
)  # not "target page ... closed": a site closing its own tab is not a crash


def _dead_browser_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _DEAD_MARKERS)


async def _active_page(session: dict):
    page = session["page"]
    if page.is_closed():
        pages = _session_pages(session)
        if pages:
            page = pages[-1]
        else:
            page = await session["context"].new_page()
            _adopt_page(session, page)
        session["page"] = page
    return page


def _parse_ref(ref: str) -> tuple[int, int] | None:
    """(revision, frame index) of "<rev>:<n>" or "<rev>:<n>@<frame>"."""
    head, _, frame = ref.partition("@")
    rev, _, number = head.partition(":")
    if not (rev.isdigit() and number.isdigit()) or (frame and not frame.isdigit()):
        return None
    return int(rev), int(frame or 0)


_READ_REV_JS = "() => document.documentElement.getAttribute('data-aiw-rev')"


async def _observe(page, revision: int) -> dict:
    snap = await page.evaluate(_OBSERVE_JS, {"rev": revision, "frame": 0})
    elements = snap["elements"]
    frames = []
    for index, frame in enumerate(page.frames[1 : MAX_FRAMES + 1], start=1):
        try:
            sub = await frame.evaluate(_OBSERVE_JS, {"rev": revision, "frame": index})
        except Exception:  # noqa: BLE001 — detached or still loading
            continue
        origin = _page_origin(frame.url)
        for element in sub["elements"][:100]:
            element["frame"] = index
            element["frame_origin"] = origin
            elements.append(element)
        frames.append({"frame": index, "url": frame.url, "elements": len(sub["elements"])})
    return {"elements": elements, "text": snap["text"], "frames": frames}


async def _desktop_step(session: dict, req: DesktopActionRequest) -> dict:
    page = await _active_page(session)

    if req.action == "observe":
        session["revision"] = session.get("revision", 0) + 1
        result = {"revision": session["revision"], **await _observe(page, session["revision"])}
        challenge = await challenges.detect(page)
        if challenge:
            # The agent hands a wall it cannot pass to its owner (desktop_handoff).
            result["challenge"] = challenge
        return result
    if req.action == "tabs":
        return {
            "tabs": [
                {"index": i, "url": p.url, "active": p is page}
                for i, p in enumerate(_session_pages(session))
            ]
        }
    if req.action == "switch_tab":
        pages = _session_pages(session)
        if req.tab is None or req.tab >= len(pages):
            return {"ok": False, "error": "tab_not_found"}
        session["page"] = pages[req.tab]
        await session["page"].bring_to_front()
        return {"switched": req.tab}
    if req.action == "navigate":
        if not req.url or not _session_host_allowed(req.url, session["allowed_hosts"]):
            return {"ok": False, "error": "host_not_allowed"}
        response = await page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
        challenge, cleared = await challenges.await_clearance(page, session, response)
        result = {"status": response.status if response else None}
        if challenge:
            result["challenge"] = {**challenge, "cleared": cleared}
        return result
    if req.action in ("back", "forward"):
        go = page.go_back if req.action == "back" else page.go_forward
        # "commit": a page restored from the back-forward cache fires no
        # load events, and waiting for them timed out on every plain back.
        response = await go(wait_until="commit", timeout=30000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:  # noqa: BLE001 — restored from cache: already loaded
            pass
        # The allowlist is checked on the page it lands on, like any navigation.
        return {"status": response.status if response else None, "moved": response is not None}
    if req.action == "read":
        return {"text": (await page.locator("body").inner_text(timeout=15000))[:200000]}
    if req.action == "downloads":
        return {
            "downloads": [
                {k: v for k, v in d.items() if k != "data"}
                for d in session.get("downloads", {}).values()
            ]
        }
    if req.action == "take_download":
        record = session.get("downloads", {}).get(req.download_id or "")
        if record is None:
            # Another session's download id names nothing here.
            return {"ok": False, "error": "download_not_found"}
        if record["status"] != "done":
            return {"ok": False, "error": f"download_{record['status']}"}
        return {
            "download": {k: v for k, v in record.items() if k != "data"},
            "content_b64": base64.b64encode(record["data"]).decode("ascii"),
        }
    if req.action == "screenshot":
        return {}
    if req.action == "press":
        if req.key not in _AGENT_KEYS:
            return {"ok": False, "error": "key_not_allowed"}
        if req.key in {"Enter", "Space"}:
            # W3: Enter in a data form is its submit, Space on its button too.
            focused = page.locator(":focus")
            if await focused.count() == 1:
                facts = await _classify(focused.first)
                if facts["class"] != "read":
                    return {
                        "ok": False,
                        "error": "needs_approval",
                        "class": facts["class"],
                        "card": await _describe(page, focused.first),
                    }
        await page.keyboard.press(" " if req.key == "Space" else req.key)
        await page.wait_for_timeout(req.wait_ms or random.randint(250, 600))
        return {"done": "press"}
    if req.action == "wait_for":
        timeout = min(req.wait_ms or 10000, 15000)
        if not req.text:
            await page.wait_for_timeout(timeout)
            return {"done": "wait_for"}
        try:
            await page.get_by_text(req.text[:200]).first.wait_for(timeout=timeout)
            return {"done": "wait_for", "found": True}
        except Exception:  # noqa: BLE001 — not there in time is an answer
            return {"done": "wait_for", "found": False}
    if req.action == "scroll" and not req.ref:
        await humanize.scroll(page, session, req.dy if req.dy is not None else 600)
        return {"done": "scroll"}

    # click / type / hover / select …: the element the observe named, on the
    # page (and frame) it saw.
    if not req.ref or req.revision is None:
        return {"ok": False, "error": "ref_and_revision_required"}
    parsed = _parse_ref(req.ref)
    if parsed is None or parsed[0] != req.revision:
        return {"ok": False, "error": "stale_ref"}
    if req.revision != session.get("revision"):
        return {"ok": False, "error": "stale_revision"}
    frame_index = parsed[1]
    if frame_index >= len(page.frames):
        return {"ok": False, "error": "stale_ref"}
    frame = page.frames[frame_index]
    try:
        seen = await frame.evaluate(_READ_REV_JS)
    except Exception as exc:  # noqa: BLE001 — the frame navigated away
        if _dead_browser_error(exc):
            raise  # E37: a dead browser is a lost session, not a stale ref
        seen = None
    if seen != str(req.revision):
        # The page changed (or navigated) since that observe.
        return {"ok": False, "error": "stale_revision"}
    target = frame.locator(f'[data-aiw-ref="{req.ref}"]')
    if await target.count() != 1:
        return {"ok": False, "error": "stale_ref"}
    if await target.evaluate("e => e.tagName") == "IFRAME":
        return {"ok": False, "error": "frame_not_actionable"}
    if req.action == "hover":
        await humanize.hover(page, target, session)
        return {"done": "hover"}
    if req.action == "scroll":
        await humanize.hover(page, target, session)
        await humanize.scroll(page, session, req.dy if req.dy is not None else 600)
        return {"done": "scroll"}
    if await target.is_disabled():
        return {"ok": False, "error": "element_disabled"}
    if req.action == "describe":
        return {"card": await _describe(page, target)}
    if req.action == "click" and req.card is not None:
        live = await _describe(page, target)
        if live["card_hash"] != _card_hash(req.card):
            # The approval was for another form state, origin or element.
            return {"ok": False, "error": "form_changed"}
    if req.action == "upload":
        if (await target.get_attribute("type") or "").lower() != "file":
            return {"ok": False, "error": "not_a_file_input"}
        payloads = [
            {
                "name": _display_name(str(f.get("name") or "")),
                "mimeType": str(f.get("mime_type") or "application/octet-stream"),
                "buffer": base64.b64decode(f.get("content_b64") or ""),
            }
            for f in req.files or []
        ]
        if not payloads:
            return {"ok": False, "error": "files_required"}
        await target.set_input_files(payloads, timeout=10000)
        return {"done": "upload", "files": [p["name"] for p in payloads]}
    if req.action == "fill_secret":
        # Exactly the secret's origin, in the main frame, into a password field.
        if not req.secret or not req.origin:
            return {"ok": False, "error": "secret_required"}
        if frame_index != 0 or _page_origin(page.url) != req.origin:
            return {"ok": False, "error": "origin_mismatch"}
        if (await target.get_attribute("type") or "").lower() != "password":
            return {"ok": False, "error": "not_a_password_field"}
        session.setdefault("secrets", set()).add(req.secret)
        await target.fill(req.secret, timeout=10000)
        return {"done": "fill_secret"}
    if req.action in {"open", "search", "select"}:
        # W3: the free actions act only on what is reading, not sending.
        facts = await _classify(target)
        if facts["class"] != "read":
            return {
                "ok": False,
                "error": "needs_approval",
                "class": facts["class"],
                "card": await _describe(page, target),
            }
        if req.action == "open":
            await humanize.click(page, target, session)
            if req.wait_ms:
                await page.wait_for_timeout(req.wait_ms)
            return {"done": "open", "class": "read", "kind": facts["kind"]}
        if req.action == "search":
            if facts["kind"] != "field" or req.text is None:
                return {"ok": False, "error": "not_a_search_field"}
            session.setdefault("typed", set()).add(req.text)
            await humanize.type_text(page, target, req.text, session)
            if req.submit:
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(req.wait_ms or random.randint(600, 1200))
            return {"done": "search", "class": "read"}
    if req.action == "select" or (
        req.action == "type" and await target.evaluate("e => e.tagName") == "SELECT"
    ):
        # A select in a data form takes the approved desktop_type path.
        if not req.text:
            return {"ok": False, "error": "text_required"}
        await humanize.click(page, target, session)
        try:
            chosen = await target.select_option(label=req.text, timeout=5000)
        except Exception:  # noqa: BLE001 — not a label: try the option value
            chosen = await target.select_option(value=req.text, timeout=5000)
        return {"done": "select", "selected": chosen}
    if req.action == "click":
        await humanize.click(page, target, session)
    else:
        session.setdefault("typed", set()).add(req.text or "")
        await humanize.type_text(page, target, req.text or "", session)
    if req.wait_ms:
        await page.wait_for_timeout(req.wait_ms)
    # A new tab the click opened becomes visible to the next "tabs".
    return {"done": req.action}


async def _human_view(session: dict) -> dict:
    page = session["page"]
    # W2: JPEG — a frame the owner's screen asks for a few times a second.
    shot = await page.screenshot(type="jpeg", quality=70, animations="disabled", caret="initial")
    return {
        "ok": True,
        "url": page.url,
        "title": await page.title(),
        "handoff": session.get("handoff"),
        "outside_allowlist": not _session_host_allowed(page.url, session["allowed_hosts"]),
        "viewport": await _viewport(page),
        "image_type": "jpeg",
        "screenshot_b64": base64.b64encode(shot).decode("ascii"),
    }


@app.post("/desktop/human")
async def desktop_human(req: DesktopHumanRequest) -> dict:
    """T3: a person looks at the session and, once it is handed to them, acts.

    The agent's actions are refused while the person holds the session; the
    person's input goes in by coordinates and keys, as a person's would. On
    return the agent must observe afresh, and a page outside the allowlist
    ends the session instead of going back to the agent.
    """
    session = _sessions.get(req.session_id)
    if session is None or session["owner"] != req.owner:
        return {"ok": False, "error": "session_not_found"}
    if not _session_alive(session):
        _drop_sessions_like(session)
        return {"ok": False, "error": "session_lost"}
    async with session["lock"]:
        if _sessions.get(req.session_id) is not session:
            return {"ok": False, "error": "session_not_found"}
        session["last_used"] = time.monotonic()
        page = await _active_page(session)
        if req.op == "take" and session.get("handoff") is None:
            session["handoff"] = {"reason": "Владелец взял сессию сам", "since": time.time()}
        if req.op not in {"view", "take"} and session.get("handoff") is None:
            return {"ok": False, "error": "session_with_agent"}
        if req.op == "click":
            if req.x is None or req.y is None:
                return {"ok": False, "error": "coordinates_required"}
            viewport = await _viewport(page)
            if req.x >= viewport["width"] or req.y >= viewport["height"]:
                return {"ok": False, "error": "outside_viewport"}
            await page.mouse.click(req.x, req.y)
            await page.wait_for_timeout(300)
        elif req.op == "type":
            if not req.text:
                return {"ok": False, "error": "text_required"}
            if len(req.text) >= _HUMAN_TEXT_REDACT_MIN:
                session.setdefault("secrets", set()).add(req.text)
            await page.keyboard.type(req.text)
        elif req.op == "key":
            if req.key not in _HUMAN_KEYS:
                return {"ok": False, "error": "key_not_allowed"}
            await page.keyboard.press(" " if req.key == "Space" else req.key)
            await page.wait_for_timeout(300)
        elif req.op == "return":
            if not _session_host_allowed(page.url, session["allowed_hosts"]):
                await _close_session(req.session_id)
                return {"ok": False, "error": "navigation_left_allowlist", "closed": True}
            session["handoff"] = None
            # Whatever the agent observed before is stale now.
            session["revision"] = session.get("revision", 0) + 1
            try:
                await page.evaluate(
                    "() => document.documentElement.removeAttribute('data-aiw-rev')"
                )
            except Exception:  # noqa: BLE001 - a page mid-navigation has no root yet
                pass
        return await _human_view(session)


@app.post("/desktop/list-owner")
async def desktop_list_owner(req: DesktopListOwnerRequest) -> dict:
    """T3: one owner's live sessions, for the person's session screen."""
    return {
        "ok": True,
        "sessions": [
            {
                "session_id": sid,
                "url": s["page"].url,
                "handoff": s.get("handoff"),
            }
            for sid, s in _sessions.items()
            if s["owner"] == req.owner and _session_pages(s)
        ],
    }


@app.post("/desktop/close-owner")
async def desktop_close_owner(req: DesktopCloseOwnerRequest) -> dict:
    """Close every session of one owner: its grant was revoked or its work canceled."""
    closed = 0
    for sid, session in list(_sessions.items()):
        if session["owner"] == req.owner:
            closed += int(await _close_session(sid))
    return {"ok": True, "closed": closed}


async def _fetch_pdf(context, req, headers, diagnostics, nav_response=None):
    """Download PDF bytes (browser cookies + human headers) and extract text."""
    status = None
    body = b""
    try:
        if nav_response is not None:
            try:
                body = await nav_response.body()
                status = nav_response.status
            except Exception:  # noqa: BLE001
                body = b""
        if not body:
            api_resp = await context.request.get(req.url, headers=headers, timeout=45000)
            status = api_resp.status
            body = await api_resp.body()
        pdf_text, pdf_title = _extract_pdf_text(body, req.max_chars)
        page_images: list[str] = []
        if not fitz:
            diagnostics.append("pdf_lib_missing")
        elif not pdf_text:
            # No text layer → likely a scanned/image PDF. Rasterize pages so the
            # backend can OCR them (this sidecar has no LLM/OCR itself).
            page_images = _render_pdf_images(body, req.pdf_ocr_pages)
            diagnostics.append(
                f"pdf_scanned_images:{len(page_images)}" if page_images else "pdf_no_text"
            )
        else:
            diagnostics.append("pdf_extracted")
        title = pdf_title or req.url.rsplit("/", 1)[-1]
    except Exception as exc:  # noqa: BLE001
        diagnostics.append(f"pdf_error:{str(exc)[:120]}")
        pdf_text, title, page_images = "", req.url.rsplit("/", 1)[-1], []
    # Caller's `finally` closes the context (returning here still runs it).
    return FetchResponse(
        final_url=req.url,
        status=status,
        title=title,
        text=pdf_text[: req.max_chars],
        screenshot_b64=None,
        truncated=len(pdf_text) > req.max_chars,
        page_images_b64=page_images,
        diagnostics=diagnostics,
    )


# The context's request API sends Chrome's own user agent; only what is wanted.
_PDF_HEADERS = {"Accept": "application/pdf,*/*;q=0.8"}


@app.post("/fetch", response_model=FetchResponse)
async def fetch(req: FetchRequest) -> FetchResponse:
    diagnostics: list[str] = []
    try:
        context, profile_key = await _context_for(req)
    except profiles.ProfileError as exc:
        return FetchResponse(diagnostics=[f"profile_error:{exc.code}"])
    except ValueError as exc:
        return FetchResponse(diagnostics=[f"bad_proxy:{str(exc)[:100]}"])
    page = await context.new_page()
    state: dict = {}
    status: int | None = None
    final_url: str | None = None
    title: str | None = None
    text = ""
    links: list[PageLink] = []
    screenshot_b64: str | None = None
    response = None
    try:
        # PDF catalogs/datasheets: Chromium aborts navigation to a direct PDF
        # link (treats it as a download), so fetch the bytes via the context's
        # request API (inherits cookies) with the full browser header set, and
        # extract text — no page render needed.
        url_is_pdf = req.url.split("?")[0].lower().endswith(".pdf")
        if url_is_pdf:
            return await _fetch_pdf(context, req, _PDF_HEADERS, diagnostics)

        # "commit" resolves as soon as the server responds — so bot-wall
        # interstitials (Cloudflare "Just a moment…") that keep reloading do
        # not hang goto; we still capture status and whatever renders.
        ctype = ""
        try:
            response = await page.goto(req.url, wait_until="commit", timeout=30000)
            if response is not None:
                status = response.status
                ctype = (response.headers or {}).get("content-type", "").lower()
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"navigation_error:{str(exc)[:160]}")

        # Server returned a PDF for a non-.pdf URL — use the navigation body.
        if "application/pdf" in ctype:
            return await _fetch_pdf(context, req, _PDF_HEADERS, diagnostics, nav_response=response)

        # Best-effort: let the DOM and client-side rendering settle. A JS
        # challenge may auto-solve during these waits; if not, we still return
        # the interstitial page rather than nothing (honest degraded result).
        for state, budget in (("domcontentloaded", 15000), ("networkidle", 8000)):
            try:
                await page.wait_for_load_state(state, timeout=budget)
            except Exception:  # noqa: BLE001
                diagnostics.append(f"{state}_timeout")

        # W2: a JS check gets time to clear in this real browser; a wall
        # that stays is reported with its kind, not read as the page.
        challenge, cleared = await challenges.await_clearance(page, state, response)
        if challenge and challenge["wall"]:
            if cleared:
                diagnostics.append(f"challenge_passed:{challenge['kind']}")
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:  # noqa: BLE001
                    pass
                # The status of the page now shown, not of the wall before it.
                status = await challenges.current_status(page) or status
            else:
                diagnostics.extend(["bot_challenge_detected", f"challenge:{challenge['kind']}"])

        # Nudge lazy-loaded content the way a reader does: pointer and wheel.
        await humanize.glance(page, state)
        await page.wait_for_timeout(req.wait_ms or random.randint(300, 900))

        try:
            final_url = page.url
            title = await page.title()
            html = await page.content()
            text = _extract_text(html, final_url or req.url)
            if not text:
                # Fallback: visible body text when extraction yields nothing.
                try:
                    text = await page.evaluate("document.body ? document.body.innerText : ''")
                except Exception:  # noqa: BLE001
                    text = ""
                diagnostics.append("used_inner_text")
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"read_error:{str(exc)[:120]}")

        # Flag a likely unsolved bot wall so the agent knows the read is partial.
        low = (text or "").lower()
        if "bot_challenge_detected" not in diagnostics and (status in (403, 503) or not text) and (
            "just a moment" in low
            or "checking your browser" in low
            or "enable javascript and cookies" in low
        ):
            diagnostics.append("bot_challenge_detected")

        if req.include_links:
            try:
                raw_links = await page.evaluate(
                    "Array.from(document.querySelectorAll('a[href]'))"
                    ".map(a => ({url: a.href, text: (a.textContent || '').trim().slice(0, 200)}))"
                )
                seen_links: set[str] = set()
                for item in raw_links or []:
                    href = str((item or {}).get("url") or "")
                    if not href.startswith(("http://", "https://")) or href in seen_links:
                        continue
                    seen_links.add(href)
                    links.append(PageLink(url=href, text=str(item.get("text") or "")))
                    if len(links) >= req.max_links:
                        break
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"links_error:{str(exc)[:120]}")

        if req.screenshot:
            try:
                shot = await page.screenshot(
                    full_page=False,
                    type="png",
                    animations="disabled",
                    caret="hide",
                    timeout=12000,
                )
                screenshot_b64 = base64.b64encode(shot).decode("ascii")
            except Exception as exc:  # noqa: BLE001
                diagnostics.append(f"screenshot_failed:{str(exc)[:80]}")
    finally:
        if profile_key is None:
            await context.close()
        else:
            # The kept profile stays open; only this read's tab goes.
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass
            _profiles.release(*profile_key)

    truncated = len(text) > req.max_chars
    return FetchResponse(
        final_url=final_url,
        status=status,
        title=title,
        text=text[: req.max_chars],
        links=links,
        screenshot_b64=screenshot_b64,
        truncated=truncated,
        diagnostics=diagnostics,
    )


# ── W2: kept profiles and a fingerprint self-check ──────────────────────────


class ProfileOwnerRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=200)


class ProfileDeleteRequest(ProfileOwnerRequest):
    name: str = Field(min_length=1, max_length=40)


@app.post("/profiles/list")
async def profiles_list(req: ProfileOwnerRequest) -> dict:
    return {"ok": True, "profiles": _profiles.list(req.owner)}


@app.post("/profiles/delete")
async def profiles_delete(req: ProfileDeleteRequest) -> dict:
    """Forget a profile: its cookies, logins and cache. Refused while in use."""
    try:
        deleted = await _profiles.delete(req.owner, req.name)
    except profiles.ProfileError as exc:
        return {"ok": False, "error": exc.code}
    return {"ok": True, "deleted": deleted}


# What a bot detector reads first. Run on a real page, in the same browser
# the agent uses, so the answer describes what sites see.
_PROBE_JS = """
async () => {
  const gl = (() => {
    try {
      const c = document.createElement('canvas').getContext('webgl');
      const ext = c && c.getExtension('WEBGL_debug_renderer_info');
      return c ? {vendor: ext ? c.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
                  renderer: ext ? c.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null} : null;
    } catch (e) { return null; }
  })();
  let notifications = null;
  try { notifications = (await navigator.permissions.query({name: 'notifications'})).state; }
  catch (e) {}
  return {
    user_agent: navigator.userAgent,
    platform: navigator.platform,
    webdriver: navigator.webdriver,
    languages: navigator.languages,
    plugins: navigator.plugins.length,
    hardware_concurrency: navigator.hardwareConcurrency,
    device_memory: navigator.deviceMemory || null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    screen: [screen.width, screen.height, screen.colorDepth],
    window: [outerWidth, outerHeight, innerWidth, innerHeight],
    chrome_object: typeof window.chrome === 'object',
    notifications_permission: notifications,
    notification_api: typeof Notification === 'undefined' ? null : Notification.permission,
    webgl: gl,
  };
}
"""

_SANNYSOFT_JS = """
() => Array.from(document.querySelectorAll('table tr')).map(tr => {
  const cells = tr.querySelectorAll('td');
  if (cells.length < 2) return null;
  return {test: cells[0].innerText.trim(), result: cells[1].innerText.trim().slice(0, 80),
          failed: cells[1].classList.contains('failed')};
}).filter(Boolean)
"""


class SelftestRequest(ProfileFields):
    url: str = Field("https://bot.sannysoft.com/", max_length=500)


@app.post("/selftest")
async def selftest(req: SelftestRequest) -> dict:
    """W2: what a site's bot detector sees in this browser, for the operator."""
    context, profile_key = await _context_for(req)
    page = await context.new_page()
    try:
        error = None
        try:
            response = await page.goto(req.url, wait_until="commit", timeout=30000)
            for state, budget in (("domcontentloaded", 15000), ("networkidle", 10000)):
                try:
                    await page.wait_for_load_state(state, timeout=budget)
                except Exception:  # noqa: BLE001
                    pass
            await challenges.await_clearance(page, {}, response)
        except Exception as exc:  # noqa: BLE001 — the test site is down: probe anyway
            response, error = None, f"navigation_error:{str(exc).splitlines()[0][:160]}"
        probe = await page.evaluate(_PROBE_JS)
        rows = []
        if "sannysoft" in req.url and error is None:
            rows = await page.evaluate(_SANNYSOFT_JS)
        return {
            "ok": True,
            "error": error,
            "url": page.url,
            "status": response.status if response else None,
            "channel": CHANNEL,
            "headless": HEADLESS,
            "probe": probe,
            "failed": [r for r in rows if r["failed"]],
            "checked": len(rows),
        }
    finally:
        if profile_key is None:
            await context.close()
        else:
            await page.close()
            _profiles.release(*profile_key)
