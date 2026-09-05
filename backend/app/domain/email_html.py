"""Sanitise inbound HTML email bodies before they are stored/served.

Primary defence for display is the sandboxed <iframe srcdoc> the client renders
into (no allow-scripts / allow-same-origin). This is defence in depth: strip
scripts, event handlers and dangerous URL schemes so a stored body is never a
live payload even if it leaks out of the iframe.

``nh3`` (ammonia) is preferred; when it is not installed (image not rebuilt
yet) a conservative regex pass is used instead.
"""

from __future__ import annotations

import re

_SCRIPTISH = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|base|form)\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SELF_CLOSING = re.compile(
    r"<\s*(script|iframe|object|embed|link|meta|base|form)\b[^>]*/?\s*>",
    re.IGNORECASE,
)
_ON_ATTR = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"(href|src|action)\s*=\s*(\"|')?\s*javascript:[^\"'>\s]*", re.IGNORECASE)


_REMOTE_IMG_SRC = re.compile(r"""(?i)\bsrc\s*=\s*(['"]?)(https?://[^'"\s>]+)\1""")
_REMOTE_BG = re.compile(
    r"""(?i)background(-image)?\s*:\s*url\(\s*['"]?(https?://[^'")]+)['"]?\s*\)"""
)


def block_remote_images(html: str | None) -> tuple[str | None, int]:
    """Defuse remote images, returning (html, blocked_count).

    A remote image in a mail body is a read receipt: the sender learns when the
    letter was opened, from which address, on which device — including for
    private mailboxes. Nothing here asked the reader first, and there was no
    setting to say no.

    The URL is preserved in ``data-blocked-src`` so "показать изображения" is a
    client-side swap and needs no re-fetch of the message.
    """
    if not html:
        return html, 0
    count = 0

    def _sub_img(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f'data-blocked-src="{match.group(2)}"'

    out = _REMOTE_IMG_SRC.sub(_sub_img, html)

    def _sub_bg(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "background-image:none"

    out = _REMOTE_BG.sub(_sub_bg, out)
    return out, count


_CID_SRC = re.compile(r"""(?i)\bsrc\s*=\s*(['"]?)cid:([^'"\s>]+)\1""")


def rewrite_cid_images(html: str | None, message_id) -> str | None:
    """Point ``src="cid:..."`` at our own inline-part endpoint.

    The sanitizer has always allowed the ``cid:`` scheme, but nothing ever
    resolved it, so every logo and inline screenshot rendered as a broken image
    — in practice, in every properly formatted business letter.
    """
    if not html or "cid:" not in html.lower():
        return html

    def _sub(match: re.Match[str]) -> str:
        from urllib.parse import quote

        cid = match.group(2).strip().strip("<>")
        return f'src="/api/email/messages/{message_id}/attachments/cid/{quote(cid)}/content"'

    return _CID_SRC.sub(_sub, html)


def sanitize_email_html(html: str | None) -> str | None:
    if not html:
        return html
    try:
        import nh3

        return nh3.clean(
            html,
            tags=nh3.ALLOWED_TAGS
            | {
                "img",
                "table",
                "thead",
                "tbody",
                "tfoot",
                "tr",
                "td",
                "th",
                "span",
                "div",
                "figure",
                "figcaption",
                "hr",
                "pre",
            },
            attributes={
                "*": {"style", "class", "align", "width", "height"},
                "a": {"href", "title", "target", "rel"},
                "img": {"src", "alt", "title", "width", "height"},
                "td": {"colspan", "rowspan", "align", "valign"},
                "th": {"colspan", "rowspan", "align", "valign"},
            },
            url_schemes={"http", "https", "mailto", "cid", "data"},
            link_rel="noopener noreferrer nofollow",
        )
    except ImportError:
        pass

    cleaned = _SCRIPTISH.sub("", html)
    cleaned = _SELF_CLOSING.sub("", cleaned)
    cleaned = _ON_ATTR.sub("", cleaned)
    cleaned = _JS_URL.sub(r"\1=#", cleaned)
    return cleaned


# ── HTML → plain text ──────────────────────────────────────────────────────

_BLOCK_TAGS = {
    "p",
    "div",
    "tr",
    "table",
    "blockquote",
    "section",
    "article",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "hr",
    "pre",
}
# Cell boundaries need a separator or a price table reads as "Итого240000".
_CELL_TAGS = {"td", "th"}
_SKIP_TAGS = {"script", "style", "head", "title", "meta", "link"}


def html_to_text(html: str | None) -> str:
    """Readable plain text from an HTML e-mail body.

    Most business mail arrives as HTML only. ``EmailMessage.body_text`` stayed
    empty for those, and everything downstream reads that column: the Russian
    full-text index (so such letters were unfindable), filter rules matching on
    ``body``, the auto-reply loop guard looking for "no-reply" markers, the
    thread-list snippet, and what the agent is handed when asked to read a
    letter — it saw an empty message and said so.

    Structure matters for all of those, so block elements become line breaks
    rather than everything collapsing into one line.
    """
    if not html:
        return ""
    try:
        from lxml import html as lxml_html

        root = lxml_html.fromstring(html)
        for bad in root.xpath("//script | //style | //head"):
            bad.getparent().remove(bad)
        parts: list[str] = []

        def _walk(node) -> None:
            tag = str(getattr(node, "tag", "") or "").lower()
            if tag in _SKIP_TAGS:
                return
            if tag == "br":
                parts.append("\n")
            elif tag == "li":
                parts.append("\n- ")
            elif tag in _CELL_TAGS:
                parts.append("\t")
            elif tag in _BLOCK_TAGS:
                parts.append("\n")
            if node.text:
                parts.append(node.text)
            for child in node:
                _walk(child)
                if child.tail:
                    parts.append(child.tail)
            if tag in _BLOCK_TAGS or tag == "li":
                parts.append("\n")

        _walk(root)
        text = "".join(parts)
    except Exception:  # noqa: BLE001 — malformed markup, or lxml unavailable
        import html as html_module

        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
        text = re.sub(r"(?i)<\s*(br|/p|/div|/tr|/li)\s*/?>", "\n", text)
        text = re.sub(r"(?i)<\s*li\b[^>]*>", "\n- ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_module.unescape(text)

    # Collapse runs of spaces/tabs, then runs of blank lines, keeping paragraphs.
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
