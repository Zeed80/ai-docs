"""Building a supplier catalog from their website when there is no file.

Э5: many suppliers publish no PDF/Excel at all — the catalog *is* the site.
This walks the supplier's own host breadth-first, ranks pages by how much they
look like a product listing, extracts rows from each with the same LLM parser
the web-source path uses, and creates entries draft-first
(provenance.discovery_method="web_crawl") so nothing lands unreviewed.

Deliberate limits: same host only, depth 3, page budget, per-request delay,
robots.txt respected, and two-level dedup (normalised URL and sha256 of the
page text) so a paginated listing that repeats content does not double the
catalog on a re-run.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from urllib.parse import urljoin, urlparse, urlunparse

import structlog

from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

PRIORITY_MARKERS = (
    "/catalog", "/katalog", "/product", "/tovar", "/price", "/prays",
    "/instrument", "/shop", "/goods",
)
PENALTY_MARKERS = (
    "/news", "/novosti", "/about", "/o-kompanii", "/contact", "/kontakt",
    "/blog", "/vacanc", "/delivery", "/dostavka", "/payment", "/login",
)
SKIP_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".css", ".js", ".ico", ".mp4",
    ".zip", ".rar", ".7z", ".doc", ".exe",
)
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_DEPTH = 3
REQUEST_DELAY_SECONDS = 0.7


def normalize_url(url: str) -> str:
    """Drop fragments, tracking params and trailing slashes for dedup."""
    parsed = urlparse(url)
    query = "&".join(
        part
        for part in parsed.query.split("&")
        if part and not part.split("=")[0].lower().startswith(("utm_", "yclid", "gclid"))
    )
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", query, ""))


def page_priority(url: str) -> int:
    """Lower sorts first. Listing-looking paths win; news/contacts lose."""
    lowered = url.lower()
    score = 0
    if any(marker in lowered for marker in PRIORITY_MARKERS):
        score -= 5
    if any(marker in lowered for marker in PENALTY_MARKERS):
        score += 10
    score += lowered.count("/")
    return score


def is_crawlable(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    return not parsed.path.lower().endswith(SKIP_SUFFIXES)


async def _robots_checker(base_url: str):
    """Return a predicate honouring robots.txt; allow-all when it is unreadable."""
    from urllib.robotparser import RobotFileParser

    import httpx

    parser = RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(robots_url)
        if resp.status_code == 200:
            parser.parse(resp.text.splitlines())
        else:
            return lambda _url: True
    except Exception as exc:  # noqa: BLE001 — a missing robots.txt is normal
        logger.info("robots_unreadable", url=robots_url, error=str(exc)[:120])
        return lambda _url: True

    return lambda url: parser.can_fetch("*", url)


@celery_app.task(
    bind=True,
    name="catalog.crawl_site",
    max_retries=0,
    soft_time_limit=5400,
    time_limit=5460,
)
def crawl_supplier_site(
    self,
    supplier_id: str,
    start_url: str,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> dict:
    from app.tasks.drawing_analysis import run_async

    return run_async(_crawl_async(supplier_id, start_url, max_pages, max_depth))


async def _crawl_async(
    supplier_id: str, start_url: str, max_pages: int, max_depth: int
) -> dict:
    from app.api.tool_catalog import _same_site
    from app.api.web_search import WebFetchRequest, fetch_page
    from app.db.models import ToolSupplier
    from app.db.session import _get_session_factory
    from app.domain.catalog_ingest_status import record_source_status
    from app.tasks.drawing_analysis import (
        _create_catalog_entries_from_rows,
        _parse_catalog_text_via_llm,
    )
    from app.vector.qdrant_store import ensure_drawing_collections

    supplier_uuid = uuid.UUID(supplier_id)
    allowed = await _robots_checker(start_url)

    queue: list[tuple[str, int]] = [(start_url, 0)]
    seen_urls: set[str] = {normalize_url(start_url)}
    seen_text: set[str] = set()
    pages_read = 0
    pages_with_rows = 0
    created_total = 0
    errors: list[str] = []

    ensure_drawing_collections()
    factory = _get_session_factory()

    while queue and pages_read < max_pages:
        queue.sort(key=lambda item: (item[1], page_priority(item[0])))
        url, depth = queue.pop(0)
        if not allowed(url):
            continue
        try:
            fetched = await fetch_page(
                WebFetchRequest(url=url, max_chars=20000, ocr=False, include_links=True)
            )
        except Exception as exc:  # noqa: BLE001 — one dead page must not end the crawl
            errors.append(f"{url}: {str(exc)[:120]}")
            continue
        pages_read += 1
        await asyncio.sleep(REQUEST_DELAY_SECONDS)

        text = (fetched.text or "").strip()
        if text:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if digest in seen_text:
                text = ""  # same content under another URL — links still useful
            else:
                seen_text.add(digest)

        if text:
            try:
                rows = await _parse_catalog_text_via_llm(text, hint=fetched.title)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"parse {url}: {str(exc)[:120]}")
                rows = []
            if rows:
                pages_with_rows += 1
                async with factory() as db:
                    supplier = await db.get(ToolSupplier, supplier_uuid)
                    if supplier is None:
                        return {"error": f"Supplier {supplier_id} not found"}
                    result = await _create_catalog_entries_from_rows(
                        db,
                        supplier_uuid,
                        rows,
                        provenance={
                            "discovery_method": "web_crawl",
                            "source_url": url,
                            "title": fetched.title,
                        },
                    )
                    await db.commit()
                created_total += result["created"]

        if depth < max_depth:
            for link in fetched.links or []:
                target = normalize_url(urljoin(url, link.url))
                if target in seen_urls or not is_crawlable(target):
                    continue
                if not _same_site(target, start_url):
                    continue
                seen_urls.add(target)
                queue.append((target, depth + 1))

    try:
        record_source_status(
            supplier_id,
            start_url,
            status="done" if created_total else "empty",
            entries_created=created_total,
            message=(
                f"Обойдено страниц: {pages_read}, с позициями: {pages_with_rows}, "
                f"создано записей: {created_total}."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — status reporting is best effort
        logger.warning("crawl_status_record_failed", error=str(exc)[:150])

    logger.info(
        "catalog_site_crawled",
        supplier_id=supplier_id,
        start_url=start_url,
        pages_read=pages_read,
        pages_with_rows=pages_with_rows,
        created=created_total,
        errors=len(errors),
    )
    return {
        "supplier_id": supplier_id,
        "start_url": start_url,
        "pages_read": pages_read,
        "pages_with_rows": pages_with_rows,
        "entries_created": created_total,
        "errors": errors[:10],
    }
