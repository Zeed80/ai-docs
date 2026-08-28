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


def sanitize_email_html(html: str | None) -> str | None:
    if not html:
        return html
    try:
        import nh3

        return nh3.clean(
            html,
            tags=nh3.ALLOWED_TAGS
            | {"img", "table", "thead", "tbody", "tfoot", "tr", "td", "th",
               "span", "div", "figure", "figcaption", "hr", "pre"},
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
