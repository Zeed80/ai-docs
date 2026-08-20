"""Ф2.A web_discover (AGENT_AUTONOMY_ROADMAP.md) — search + read for
exploratory discovery, scoped by the same ComputerUseGrant as browser_fetch.
Split out of test_computer_use_grants.py (execute) since this exercises a
different endpoint with its own search/fetch orchestration; _host_allowed's
"*" wildcard is tested here too since web_discover is what motivated it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.computer_use import _host_allowed
from app.domain.work_orders import claim_ready_step, create_single_step_plan, create_work_order
from app.tasks.work_orders import ApprovalRequiredError, execute_claimed_step


# ── _host_allowed wildcard ────────────────────────────────────────────────


class TestHostAllowedWildcard:
    def test_wildcard_allows_any_host(self):
        assert _host_allowed("https://random-supplier.example/catalog", ["*"]) is True

    def test_wildcard_still_requires_http_scheme(self):
        assert _host_allowed("ftp://random-supplier.example/catalog", ["*"]) is False

    def test_empty_list_still_allows_nothing(self):
        """The empty-list default must stay "nothing allowed" — "*" is an
        explicit opt-in, not a side effect of an unset allowlist."""
        assert _host_allowed("https://anything.example", []) is False

    def test_named_host_still_works_without_wildcard(self):
        assert _host_allowed("https://sub.acme.example/x", ["acme.example"]) is True
        assert _host_allowed("https://other.example/x", ["acme.example"]) is False

    def test_wildcard_mixed_with_named_hosts_still_allows_any(self):
        assert _host_allowed("https://anything.example", ["acme.example", "*"]) is True


# ── web_discover endpoint (needs live Postgres — client fixture) ──────────


@pytest.mark.asyncio
async def test_web_discover_requires_a_grant(client: AsyncClient):
    created = await client.post("/api/work-orders", json={"objective": "Найди каталоги поставщиков"})
    assert created.status_code == 201
    order_id = created.json()["id"]

    resp = await client.post(
        "/api/computer-use/web-discover",
        json={"work_order_id": order_id, "queries": ["каталог инструмента"]},
    )
    assert resp.status_code == 423


@pytest.mark.asyncio
async def test_web_discover_fetches_only_allowed_hosts_and_skips_the_rest(client: AsyncClient):
    created = await client.post("/api/work-orders", json={"objective": "Найди каталоги поставщиков"})
    order_id = created.json()["id"]
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={"actions": ["browser_fetch"], "allowed_hosts": ["acme.example"], "max_actions": 10, "reason": "test"},
    )
    assert granted.status_code == 201, granted.text

    from app.api.web_search import WebFetchResponse, WebSearchResponse

    search_response = WebSearchResponse(
        query="q",
        provider="searxng",
        results=[
            _mock_search_result_pydantic("https://acme.example/catalog", "on-allowlist"),
            _mock_search_result_pydantic("https://other.example/catalog", "off-allowlist"),
        ],
    )
    fetch_response = WebFetchResponse(
        url="https://acme.example/catalog", final_url="https://acme.example/catalog",
        status=200, title="ACME Catalog", text="каталог инструмента", truncated=False, diagnostics=[],
    )

    with (
        patch("app.api.web_search.execute_web_search", new=AsyncMock(return_value=search_response)),
        patch("app.api.web_search.fetch_page", new=AsyncMock(return_value=fetch_response)),
    ):
        resp = await client.post(
            "/api/computer-use/web-discover",
            json={"work_order_id": order_id, "queries": ["каталог инструмента"]},
        )

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["fetched"]) == 1
    assert data["fetched"][0]["url"] == "https://acme.example/catalog"
    assert data["fetched"][0]["text"] == "каталог инструмента"
    assert len(data["skipped"]) == 1
    assert data["skipped"][0]["url"] == "https://other.example/catalog"
    assert data["skipped"][0]["reason"] == "host_not_allowed"


@pytest.mark.asyncio
async def test_web_discover_wildcard_grant_fetches_any_host(client: AsyncClient):
    created = await client.post("/api/work-orders", json={"objective": "Найди каталоги поставщиков"})
    order_id = created.json()["id"]
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={"actions": ["browser_fetch"], "allowed_hosts": ["*"], "max_actions": 10, "reason": "exploratory"},
    )
    assert granted.status_code == 201, granted.text

    from app.api.web_search import WebFetchResponse, WebSearchResponse

    search_response = WebSearchResponse(
        query="q", provider="searxng",
        results=[_mock_search_result_pydantic("https://never-seen-before.example/x", "s")],
    )
    fetch_response = WebFetchResponse(
        url="https://never-seen-before.example/x", status=200, title="T", text="body", diagnostics=[],
    )

    with (
        patch("app.api.web_search.execute_web_search", new=AsyncMock(return_value=search_response)),
        patch("app.api.web_search.fetch_page", new=AsyncMock(return_value=fetch_response)),
    ):
        resp = await client.post(
            "/api/computer-use/web-discover",
            json={"work_order_id": order_id, "queries": ["что угодно"]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["fetched"]) == 1
    assert not data["skipped"]


@pytest.mark.asyncio
async def test_web_discover_stops_fetching_once_budget_exhausted(client: AsyncClient):
    created = await client.post("/api/work-orders", json={"objective": "Найди каталоги поставщиков"})
    order_id = created.json()["id"]
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={"actions": ["browser_fetch"], "allowed_hosts": ["*"], "max_actions": 1, "reason": "tight budget"},
    )
    assert granted.status_code == 201, granted.text

    from app.api.web_search import WebFetchResponse, WebSearchResponse

    search_response = WebSearchResponse(
        query="q", provider="searxng",
        results=[
            _mock_search_result_pydantic("https://a.example/1", "a"),
            _mock_search_result_pydantic("https://b.example/2", "b"),
        ],
    )

    async def _fake_fetch(payload):
        return WebFetchResponse(url=payload.url, status=200, title="T", text="body", diagnostics=[])

    with (
        patch("app.api.web_search.execute_web_search", new=AsyncMock(return_value=search_response)),
        patch("app.api.web_search.fetch_page", new=AsyncMock(side_effect=_fake_fetch)),
    ):
        resp = await client.post(
            "/api/computer-use/web-discover",
            json={"work_order_id": order_id, "queries": ["q"], "max_sources": 10},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["fetched"]) == 1  # only max_actions=1 worth
    assert len(data["skipped"]) == 1
    assert data["skipped"][0]["reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_web_discover_search_error_does_not_fail_the_whole_call(client: AsyncClient):
    created = await client.post("/api/work-orders", json={"objective": "Найди каталоги поставщиков"})
    order_id = created.json()["id"]
    granted = await client.post(
        f"/api/work-orders/{order_id}/computer-grants",
        json={"actions": ["browser_fetch"], "allowed_hosts": ["*"], "max_actions": 5, "reason": "test"},
    )
    assert granted.status_code == 201

    with patch("app.api.web_search.execute_web_search", new=AsyncMock(side_effect=RuntimeError("searx down"))):
        resp = await client.post(
            "/api/computer-use/web-discover",
            json={"work_order_id": order_id, "queries": ["q1"]},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["fetched"] == []
    assert any("searx down" in d for d in data["search_diagnostics"])


def _mock_search_result_pydantic(url: str, snippet: str):
    from app.api.web_search import WebSearchResult

    return WebSearchResult(title=url, url=url, snippet=snippet)


# ── Ф2.B: notify-before-scope when computer_use needs a grant ─────────────


@pytest.mark.asyncio
async def test_computer_use_approval_required_also_notifies_owner_about_the_grant(test_engine):
    """A computer_use step failing with ApprovalRequiredError must both
    create the usual digest-Approval (unchanged, existing behaviour for every
    other gated capability) AND send a distinct notification — deciding the
    Approval alone would not create the ComputerUseGrant this actually needs.
    """
    from app.db.models import Notification

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(
            db, owner_key="alice", objective="Найди каталоги поставщиков режущего инструмента"
        )
        await create_single_step_plan(
            db,
            order,
            kind="capability",
            title="Discover",
            input_data={},
            capability="computer_use",
            action="web_discover",
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id = step.id, attempt.id
        await db.commit()

    with patch(
        "app.tasks.work_orders._execute_step_kind",
        new=AsyncMock(
            side_effect=ApprovalRequiredError("computer_use", "web_discover", {"queries": ["x"]})
        ),
    ):
        result = await execute_claimed_step(
            step_id, attempt_id, schedule_verification=False, session_factory=factory
        )
    assert result is False

    async with factory() as db:
        notif = (
            await db.execute(
                select(Notification).where(
                    Notification.entity_id == order_id,
                    Notification.source_task == "workorder.needs_computer_use_grant",
                )
            )
        ).scalar_one_or_none()
        assert notif is not None
        assert notif.user_sub == "alice"
        assert "computer-grants" in notif.body


@pytest.mark.asyncio
async def test_non_computer_use_approval_required_does_not_send_the_grant_notification(test_engine):
    """Regression guard: the extra notification is specific to
    capability=="computer_use" — every other gated capability keeps behaving
    exactly as before (digest-Approval only, no notification)."""
    from app.db.models import Notification

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        order = await create_work_order(db, owner_key="bob", objective="Одобрить накладную")
        await create_single_step_plan(
            db,
            order,
            kind="capability",
            title="Approve",
            input_data={},
            capability="invoices",
            action="approve",
        )
        order_id = order.id
        await db.commit()

    async with factory() as db:
        claimed = await claim_ready_step(db, worker_id="w1", work_order_id=order_id)
        assert claimed is not None
        _order, step, attempt = claimed
        step_id, attempt_id = step.id, attempt.id
        await db.commit()

    with patch(
        "app.tasks.work_orders._execute_step_kind",
        new=AsyncMock(side_effect=ApprovalRequiredError("invoices", "approve", {})),
    ):
        await execute_claimed_step(step_id, attempt_id, schedule_verification=False, session_factory=factory)

    async with factory() as db:
        notif = (
            await db.execute(
                select(Notification).where(Notification.entity_id == order_id)
            )
        ).scalar_one_or_none()
        assert notif is None
