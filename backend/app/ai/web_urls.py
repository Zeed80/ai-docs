"""URL identity and near-duplicate text for web research.

Two links are the same page when they differ only by tracking parameters,
scheme, ``www.``/``m.``/``amp.`` host prefix, an ``/amp`` suffix, a fragment or
a trailing slash. Two pages are the same text when their simhash fingerprints
are a few bits apart (mirrors, print versions, syndicated copies).
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

# Query parameters that carry attribution, not content.
_TRACKING = re.compile(
    r"^(utm_\w+|gclid|gclsrc|dclid|fbclid|yclid|ysclid|msclkid|mc_cid|mc_eid|_openstat|"
    r"ref_src|igshid|spm|srsltid|_ga|_gl|cmpid|roistat\w*)$",
    re.I,
)
_HOST_PREFIX = re.compile(r"^(www\d*|m|mobile|amp)\.")


def url_key(url: str) -> str:
    """Canonical identity of a URL for de-duplication (not for fetching)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (parts.hostname or "").lower()
    while _HOST_PREFIX.match(host) and host.count(".") > 1:
        host = _HOST_PREFIX.sub("", host, count=1)
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = re.sub(r"/+", "/", parts.path or "/")
    path = re.sub(r"/amp/?$|\.amp$", "", path) or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING.match(k)
        )
    )
    return f"{host}{path}" + (f"?{query}" if query else "")


def dedupe(urls: list[str], cap: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        key = url_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= cap:
            break
    return out


_WORD = re.compile(r"\w+", re.U)


def simhash(text: str) -> int:
    """64-bit simhash over word 3-shingles."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    if len(words) < 3:
        shingles = [" ".join(words)] if words else []
    else:
        shingles = [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
    vector = [0] * 64
    for shingle in shingles:
        h = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if h >> bit & 1 else -1
    return sum(1 << bit for bit in range(64) if vector[bit] > 0)


# Hamming distance at or below which two texts are the same page.
NEAR_DUPLICATE_BITS = 3


def near_duplicate(a: int, b: int) -> bool:
    return bin(a ^ b).count("1") <= NEAR_DUPLICATE_BITS
