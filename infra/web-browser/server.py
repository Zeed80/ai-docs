"""Human-like page fetcher.

A tiny FastAPI service that drives a stealth-patched headless Chromium
(``patchright``) so pages load the way a person's browser would: JavaScript
executes, ``navigator.webdriver`` is hidden, a realistic fingerprint / locale /
timezone is presented, and basic bot walls (JS challenges, cookie gates) clear
on their own. It returns the rendered page's readable text and, on request, a
screenshot.

This service is the ONLY component with outbound internet access for browsing;
the backend proxies to it over the internal Docker network.
"""

from __future__ import annotations

import asyncio
import base64
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
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

# A believable desktop Chrome fingerprint. Kept in sync-ish with the bundled
# Chromium major so the UA does not contradict the real engine.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_VIEWPORT = {"width": 1366, "height": 900}
_LOCALE = "ru-RU"
_TIMEZONE = "Europe/Moscow"
_EXTRA_HEADERS = {
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Upgrade-Insecure-Requests": "1",
}

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


class _Browser:
    """Lazily-started shared Chromium; one fresh context per fetch."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def ensure(self):
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True, args=_LAUNCH_ARGS
                )
            return self._browser

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None


_engine = _Browser()
_sessions: dict[str, dict] = {}

# E31: a desktop session belongs to the work that started it. Every action
# names its owner; a wrong owner reads exactly like a missing session, so a
# known session id gives nothing. Sessions end on their own.
SESSION_IDLE_SECONDS = int(os.environ.get("BROWSER_SESSION_IDLE_SECONDS", "600"))
SESSION_MAX_SECONDS = int(os.environ.get("BROWSER_SESSION_MAX_SECONDS", "1800"))
MAX_SESSIONS = int(os.environ.get("BROWSER_MAX_SESSIONS", "8"))
MAX_SESSIONS_PER_OWNER = int(os.environ.get("BROWSER_MAX_SESSIONS_PER_OWNER", "2"))
MAX_TABS = int(os.environ.get("BROWSER_MAX_TABS", "4"))


def _expired(session: dict, now: float) -> bool:
    return (
        now - session["last_used"] > SESSION_IDLE_SECONDS
        or now - session["created"] > SESSION_MAX_SECONDS
    )


async def _close_session(session_id: str) -> bool:
    session = _sessions.pop(session_id, None)
    if session is None:
        return False
    try:
        await session["context"].close()
    except Exception:  # noqa: BLE001 — the context may already be gone
        pass
    return True


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
        except Exception:  # noqa: BLE001 — keep sweeping
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweeper = asyncio.create_task(_sweeper())
    yield
    sweeper.cancel()
    for sid in list(_sessions):
        await _close_session(sid)
    await _engine.close()


app = FastAPI(title="web-browser", lifespan=lifespan)


class FetchRequest(BaseModel):
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


class DesktopStartRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    allowed_hosts: list[str] = Field(min_length=1, max_length=50)
    owner: str = Field(min_length=1, max_length=200)


class DesktopCloseOwnerRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=200)


class DesktopActionRequest(BaseModel):
    session_id: str
    owner: str = Field(min_length=1, max_length=200)
    action: str = Field(
        pattern="^(observe|click|type|read|screenshot|navigate|tabs|switch_tab|close)$"
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


# E32: what an observe sees. Interactive elements of the active tab's main
# frame get a ref "<revision>:<n>" written into the DOM; a MutationObserver
# marks the page dirty on any structural change after that, so an action under
# an old revision is refused instead of hitting whatever is there now.
# Elements inside iframes are listed as the frame only — not addressable.
_OBSERVE_JS = """
(rev) => {
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
    const ref = rev + ':' + (i + 1);
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
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme in {"http", "https"} and any(
        host == allowed.casefold() or host.endswith("." + allowed.casefold())
        for allowed in hosts
    )


async def _new_browser_context():
    browser = await _engine.ensure()
    return await browser.new_context(
        user_agent=_USER_AGENT,
        viewport=_VIEWPORT,
        locale=_LOCALE,
        timezone_id=_TIMEZONE,
        extra_http_headers=_EXTRA_HEADERS,
        java_script_enabled=True,
        ignore_https_errors=True,
    )


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
    # A fresh context per session: no cookies, storage or cache of anyone else.
    context = await _new_browser_context()

    def _cap_tabs(new_page) -> None:
        if len(context.pages) > MAX_TABS:
            asyncio.create_task(new_page.close())

    context.on("page", _cap_tabs)
    page = await context.new_page()
    try:
        response = await page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        response = None
    if not _session_host_allowed(page.url, req.allowed_hosts):
        await context.close()
        return {"ok": False, "error": "navigation_left_allowlist"}
    session_id = str(uuid.uuid4())
    now = time.monotonic()
    _sessions[session_id] = {
        "context": context,
        "page": page,
        "allowed_hosts": list(req.allowed_hosts),
        "lock": asyncio.Lock(),
        "owner": req.owner,
        "created": now,
        "last_used": now,
    }
    shot = await page.screenshot(type="png", animations="disabled", caret="initial")
    return {
        "ok": True,
        "session_id": session_id,
        "url": page.url,
        "status": response.status if response else None,
        "title": await page.title(),
        "screenshot_b64": base64.b64encode(shot).decode("ascii"),
    }


@app.post("/desktop/action")
async def desktop_action(req: DesktopActionRequest) -> dict:
    session = _sessions.get(req.session_id)
    # Another owner's session answers exactly like a missing one.
    if session is None or session["owner"] != req.owner:
        return {"ok": False, "error": "session_not_found"}
    if _expired(session, time.monotonic()):
        await _close_session(req.session_id)
        return {"ok": False, "error": "session_expired"}
    async with session["lock"]:
        if _sessions.get(req.session_id) is not session:
            # Closed (by owner, revoke or sweep) while this action waited.
            return {"ok": False, "error": "session_not_found"}
        session["last_used"] = time.monotonic()
        if req.action == "close":
            await _close_session(req.session_id)
            return {"ok": True, "closed": True}
        try:
            result = await _desktop_step(session, req)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"action_failed:{str(exc)[:300]}"}
        if result.get("ok") is False:
            return result
        page = session["page"]
        if not _session_host_allowed(page.url, session["allowed_hosts"]):
            await _close_session(req.session_id)
            return {"ok": False, "error": "navigation_left_allowlist"}
        shot = await page.screenshot(type="png", animations="disabled", caret="initial")
        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "tab": session["context"].pages.index(page),
            # E32: what the page says is data from the site, never an
            # instruction to the agent or a change of its owner or rights.
            "content_trust": "untrusted_page",
            **result,
            "screenshot_b64": base64.b64encode(shot).decode("ascii"),
        }


async def _desktop_step(session: dict, req: DesktopActionRequest) -> dict:
    context = session["context"]
    page = session["page"]
    if page.is_closed():
        page = session["page"] = context.pages[-1] if context.pages else await context.new_page()

    if req.action == "observe":
        session["revision"] = session.get("revision", 0) + 1
        snap = await page.evaluate(_OBSERVE_JS, session["revision"])
        return {
            "revision": session["revision"],
            "elements": snap["elements"],
            "text": snap["text"],
            "frames": len(page.frames) - 1,
        }
    if req.action == "tabs":
        return {
            "tabs": [
                {"index": i, "url": p.url, "active": p is page}
                for i, p in enumerate(context.pages)
            ]
        }
    if req.action == "switch_tab":
        if req.tab is None or req.tab >= len(context.pages):
            return {"ok": False, "error": "tab_not_found"}
        session["page"] = context.pages[req.tab]
        await session["page"].bring_to_front()
        return {"switched": req.tab}
    if req.action == "navigate":
        if not req.url or not _session_host_allowed(req.url, session["allowed_hosts"]):
            return {"ok": False, "error": "host_not_allowed"}
        response = await page.goto(req.url, wait_until="domcontentloaded", timeout=30000)
        return {"status": response.status if response else None}
    if req.action == "read":
        return {"text": (await page.locator("body").inner_text(timeout=15000))[:200000]}
    if req.action == "screenshot":
        return {}

    # click / type: the element the observe named, on the page it saw.
    if not req.ref or req.revision is None:
        return {"ok": False, "error": "ref_and_revision_required"}
    if req.revision != session.get("revision"):
        return {"ok": False, "error": "stale_revision"}
    seen = await page.evaluate("() => document.documentElement.getAttribute('data-aiw-rev')")
    if seen != str(req.revision):
        # The page changed (or navigated) since that observe.
        return {"ok": False, "error": "stale_revision"}
    target = page.locator(f'[data-aiw-ref="{req.ref}"]')
    if await target.count() != 1:
        return {"ok": False, "error": "stale_ref"}
    if await target.evaluate("e => e.tagName") == "IFRAME":
        return {"ok": False, "error": "frame_not_actionable"}
    if await target.is_disabled():
        return {"ok": False, "error": "element_disabled"}
    if req.action == "click":
        await target.click(timeout=10000)
    else:
        await target.fill(req.text or "", timeout=10000)
    if req.wait_ms:
        await page.wait_for_timeout(req.wait_ms)
    # A new tab the click opened becomes visible to the next "tabs".
    return {"done": req.action}


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


@app.post("/fetch", response_model=FetchResponse)
async def fetch(req: FetchRequest) -> FetchResponse:
    diagnostics: list[str] = []
    context = await _new_browser_context()
    page = await context.new_page()
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
            pdf_headers = {"User-Agent": _USER_AGENT, **_EXTRA_HEADERS}
            return await _fetch_pdf(context, req, pdf_headers, diagnostics)

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
            pdf_headers = {"User-Agent": _USER_AGENT, **_EXTRA_HEADERS}
            return await _fetch_pdf(
                context, req, pdf_headers, diagnostics, nav_response=response
            )

        # Best-effort: let the DOM and client-side rendering settle. A JS
        # challenge may auto-solve during these waits; if not, we still return
        # the interstitial page rather than nothing (honest degraded result).
        for state, budget in (("domcontentloaded", 15000), ("networkidle", 8000)):
            try:
                await page.wait_for_load_state(state, timeout=budget)
            except Exception:  # noqa: BLE001
                diagnostics.append(f"{state}_timeout")

        # Nudge lazy-loaded content and mimic a human glance.
        try:
            await page.mouse.move(random.randint(200, 900), random.randint(200, 600))
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight/3)")
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(req.wait_ms or random.randint(300, 900))

        try:
            final_url = page.url
            title = await page.title()
            html = await page.content()
            text = _extract_text(html, final_url or req.url)
            if not text:
                # Fallback: visible body text when extraction yields nothing.
                try:
                    text = await page.evaluate(
                        "document.body ? document.body.innerText : ''"
                    )
                except Exception:  # noqa: BLE001
                    text = ""
                diagnostics.append("used_inner_text")
        except Exception as exc:  # noqa: BLE001
            diagnostics.append(f"read_error:{str(exc)[:120]}")

        # Flag a likely unsolved bot wall so the agent knows the read is partial.
        low = (text or "").lower()
        if (status in (403, 503) or not text) and (
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
        await context.close()

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
