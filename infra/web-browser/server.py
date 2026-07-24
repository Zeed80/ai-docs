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
import random
from contextlib import asynccontextmanager

from fastapi import FastAPI
from patchright.async_api import async_playwright
from pydantic import BaseModel, Field

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await _engine.close()


app = FastAPI(title="web-browser", lifespan=lifespan)


class FetchRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2048)
    screenshot: bool = False
    max_chars: int = Field(20000, ge=500, le=200000)
    wait_ms: int = Field(0, ge=0, le=15000)
    # For a scanned PDF (no text layer): how many pages to rasterize for OCR.
    pdf_ocr_pages: int = Field(5, ge=0, le=20)


class FetchResponse(BaseModel):
    final_url: str | None = None
    status: int | None = None
    title: str | None = None
    text: str = ""
    screenshot_b64: str | None = None
    truncated: bool = False
    # Base64 PNGs of scanned-PDF pages, for the backend to OCR.
    page_images_b64: list[str] = []
    diagnostics: list[str] = []


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
    browser = await _engine.ensure()
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport=_VIEWPORT,
        locale=_LOCALE,
        timezone_id=_TIMEZONE,
        extra_http_headers=_EXTRA_HEADERS,
        java_script_enabled=True,
        ignore_https_errors=True,
    )
    page = await context.new_page()
    status: int | None = None
    final_url: str | None = None
    title: str | None = None
    text = ""
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
        screenshot_b64=screenshot_b64,
        truncated=truncated,
        diagnostics=diagnostics,
    )
