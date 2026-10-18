"""browser_fetch hands the model the page's links.

E52: a catalog page listed items whose prices sat on linked pages. The fetch
never asked the browser for links, so the model got ``"links": []``, could not
follow them and wrote prices it had never read.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.api import computer_use, web_search


def test_browser_fetch_asks_for_the_page_links(monkeypatch):
    seen = {}

    async def fake_fetch(request):
        seen["request"] = request
        return web_search.WebFetchResponse(
            url=request.url,
            final_url=request.url,
            status=200,
            text="Каталог\nШина медная",
            links=[web_search.PageLink(url="http://eval-sites/med/bus.html", text="Шина медная")],
        )

    monkeypatch.setattr(web_search, "fetch_page", fake_fetch)
    grant = SimpleNamespace(allowed_hosts=["eval-sites"], work_order_id=None)

    result, _ = asyncio.run(
        computer_use._perform("browser_fetch", "http://eval-sites/med/catalog.html", {}, grant)
    )

    assert seen["request"].include_links is True
    assert result["links"] == [{"url": "http://eval-sites/med/bus.html", "text": "Шина медная"}]
