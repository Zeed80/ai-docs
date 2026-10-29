"""W2: recognise anti-bot walls and give a JS check time to pass on its own.

A wall is a page that stands in for the one asked for: Cloudflare's "Just a
moment…", DDoS-Guard, Qrator, Yandex SmartCaptcha, a bare CAPTCHA page. A
CAPTCHA *widget* inside an ordinary page (a contact form) is not a wall.

The browser never solves a CAPTCHA itself. A JS check usually clears in a
real Chrome with a kept profile within seconds; whatever does not clear is
reported with its kind so the agent hands the session to its owner.
"""

from __future__ import annotations

import asyncio
import random

import humanize

# Seconds a JS check is given to clear before the wall is reported.
CLEARANCE_WAIT_S = 15.0

# Iframes of challenge and CAPTCHA providers, by kind.
_FRAME_HOSTS = (
    ("challenges.cloudflare.com", "cloudflare_turnstile"),
    ("hcaptcha.com", "hcaptcha"),
    ("google.com/recaptcha", "recaptcha"),
    ("recaptcha.net", "recaptcha"),
    ("smartcaptcha.yandexcloud.net", "yandex_smartcaptcha"),
    ("captcha-api.yandex.ru", "yandex_smartcaptcha"),
)

# Facts of the page itself: (kind, JS expression true on such a wall).
_PAGE_PROBE = """
() => {
  const html = document.documentElement ? document.documentElement.outerHTML.slice(0, 200000) : '';
  const text = document.body ? document.body.innerText : '';
  return {
    title: document.title || '',
    text_len: text.trim().length,
    cf: !!(window._cf_chl_opt || document.querySelector('#challenge-form, #cf-challenge-running, #challenge-running, [name=cf-turnstile-response]')),
    cf_block: !!document.querySelector('#cf-error-details, .cf-error-code, #cf-wrapper .cf-error-type')
      || (document.title || '').startsWith('Attention Required! | Cloudflare'),
    ddg: html.includes('ddos-guard') || !!document.querySelector('[id^=ddg-], #ddg-captcha'),
    qrator: html.includes('__qrator') || html.includes('qauth'),
    ya_wall: location.pathname.startsWith('/showcaptcha') || !!document.querySelector('form[action*="checkcaptcha"], .CheckboxCaptcha, .AdvancedCaptcha'),
  };
}
"""

# A page this short with a challenge marker is the challenge, not content.
_WALL_TEXT_MAX = 1500
# An error status with almost no text: some site's own JS check (Wildberries
# answers 498 «Проверяем браузер», Ozon 403) — or a plain refusal; it is given
# the same wait, and only a document that then loads with a good status counts.
_UNKNOWN_CHECK_TEXT_MAX = 300
_BLOCKED_STATUSES = (401, 403, 429, 498, 503)

_NAV_STATUS_JS = """
() => { const e = performance.getEntriesByType('navigation')[0];
        return e && e.responseStatus ? e.responseStatus : null; }
"""


async def current_status(page) -> int | None:
    """HTTP status of the document now shown (after a check reloaded it)."""
    try:
        return await page.evaluate(_NAV_STATUS_JS)
    except Exception:  # noqa: BLE001
        return None


async def detect(page, response=None) -> dict | None:
    """``{"kind", "wall"}`` when the page shows a challenge; None otherwise."""
    try:
        facts = await page.evaluate(_PAGE_PROBE)
    except Exception:  # noqa: BLE001 — navigating: nothing to judge yet
        return None
    status = (
        getattr(response, "status", None) if response is not None else await current_status(page)
    )
    server = ""
    if response is not None:
        try:
            server = ((response.headers or {}).get("server") or "").lower()
        except Exception:  # noqa: BLE001
            server = ""
    short = facts["text_len"] < _WALL_TEXT_MAX
    blocked_status = status in _BLOCKED_STATUSES

    if facts["cf_block"]:
        # Cloudflare refused this address (WAF, IP reputation): no check to
        # wait out — another exit (proxy) or the owner can get through.
        return {"kind": "cloudflare_block", "wall": True, "clearable": False}
    kind = None
    if facts["cf"] or ("cloudflare" in server and blocked_status):
        kind = "cloudflare"
    elif facts["ddg"] or "ddos-guard" in server:
        kind = "ddos_guard" if (short or blocked_status) else None
    elif facts["qrator"] or "qrator" in server:
        kind = "qrator" if (short or blocked_status) else None
    elif facts["ya_wall"]:
        kind = "yandex_smartcaptcha"
    if kind:
        return {"kind": kind, "wall": True}

    for frame in page.frames[1:]:
        url = frame.url or ""
        for marker, frame_kind in _FRAME_HOSTS:
            if marker in url:
                # A CAPTCHA on a page with real content is a form widget.
                return {"kind": frame_kind, "wall": short or blocked_status}
    if blocked_status and short and "captcha" in facts["title"].lower():
        return {"kind": "captcha", "wall": True}
    if blocked_status and facts["text_len"] < _UNKNOWN_CHECK_TEXT_MAX:
        return {"kind": "unknown_check", "wall": True}
    return None


async def await_clearance(page, state: dict, response=None) -> tuple[dict | None, bool]:
    """(challenge seen, cleared). Waits only while a wall is up."""
    seen = await detect(page, response)
    if not seen or not seen["wall"]:
        return seen, True
    if seen.get("clearable") is False:
        return seen, False
    deadline = asyncio.get_running_loop().time() + CLEARANCE_WAIT_S
    while asyncio.get_running_loop().time() < deadline:
        # A person waits and moves the pointer; a check watches for that.
        await asyncio.sleep(random.uniform(0.8, 1.6))
        try:
            await humanize.move_to(page, state, random.uniform(200, 900), random.uniform(200, 600))
        except Exception:  # noqa: BLE001 — the page is reloading itself
            pass
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:  # noqa: BLE001
            pass
        now = await detect(page)
        if now is None or not now["wall"]:
            # Cleared only if the document now shown loaded well: a check
            # that turns into a refusal page has not been passed.
            status = await current_status(page)
            if status is None or status < 400:
                return seen, True
            return seen, False
    return seen, False
