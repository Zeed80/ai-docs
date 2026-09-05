"""Page-level helpers for catalog ingestion: cost gate, hashes, storage paths.

The page is the unit of work, of progress and of idempotency. Everything here
is pure: the Celery task owns the IO, these functions decide *what* to do with
a page and *how to name* what comes out of it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# An article code: has both letters and digits, at least four characters
# ("MT190-016C04", "2873-101", "DR-6.5").
_CODE_RE = re.compile(r"\b(?=[A-ZА-Я0-9./-]*\d)(?=[A-ZА-Я0-9./-]*[A-ZА-Я])[A-ZА-Я0-9./-]{4,}\b")
_MEASURE_RE = re.compile(
    r"\d+[.,]?\d*\s*(?:мм|mm|см|cm|м\b|шт|pcs|кг|kg|руб|₽|rub|eur|usd|°)", re.IGNORECASE
)
# "Сверло спиральное ......... 245" — a contents line.
_DOT_LEADER_RE = re.compile(r"\.{4,}\s*\d+\s*$")

MIN_TEXT_CHARS = 40


@dataclass
class PageVerdict:
    parse: bool
    skip_reason: str | None = None
    codes: int = 0
    measures: int = 0


def page_product_verdict(text: str | None) -> PageVerdict:
    """Is this page worth an LLM call?

    Measured cost of being wrong in the other direction: an LLM call is 13-40 s
    on the shared GPU, and a 948-page catalog opens with covers, ~30 pages of
    contents and section dividers. Skipping those is the difference between a
    2-hour and a 3-hour run — and the page image is still rendered, so nothing
    becomes invisible to the user.
    """
    body = (text or "").strip()
    if len(body) < MIN_TEXT_CHARS:
        return PageVerdict(parse=False, skip_reason="blank")

    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if lines:
        leaders = sum(1 for line in lines if _DOT_LEADER_RE.search(line))
        if leaders / len(lines) > 0.3:
            return PageVerdict(parse=False, skip_reason="toc")

    codes = len(set(_CODE_RE.findall(body)))
    measures = len(_MEASURE_RE.findall(body))
    if codes >= 2 or measures >= 3 or (codes >= 1 and measures >= 1):
        return PageVerdict(parse=True, codes=codes, measures=measures)
    return PageVerdict(
        parse=False, skip_reason="no_product_signals", codes=codes, measures=measures
    )


def entry_content_hash(
    document_id, page_number: int, part_number: str | None, name: str | None
) -> str:
    """Stable identity of a position within its catalog.

    Re-running a batch (a restart, a manual re-parse) must not create a second
    copy of the same row — this hash is what makes the insert idempotent.
    """
    key = (part_number or name or "").lower().replace("ё", "е")
    key = re.sub(r"[^a-zа-я0-9]+", "", key)
    raw = f"{document_id}:{page_number}:{key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def catalog_pages_prefix(storage_path: str) -> str:
    """Where a catalog's page images live, next to the file itself.

    `storage_path` is tool-catalogs/{supplier}/{d2}/{digest}/{filename}; the
    images go into sibling folders so deleting the catalog is one delete_prefix.
    """
    return storage_path.rsplit("/", 1)[0]


def page_image_path(storage_path: str, page_number: int, *, thumb: bool = False) -> str:
    suffix = "_thumb" if thumb else ""
    return f"{catalog_pages_prefix(storage_path)}/pages/{page_number:04d}{suffix}.webp"


def crop_image_path(storage_path: str, page_number: int, key: str, *, thumb: bool = False) -> str:
    suffix = "_thumb" if thumb else ""
    safe_key = re.sub(r"[^a-z0-9]+", "", key.lower()) or "0"
    return f"{catalog_pages_prefix(storage_path)}/crops/{page_number:04d}_{safe_key}{suffix}.webp"
