"""T4: a call's cost is fixed by the tariff in force when it was reserved.

Before, every receipt said "tariff_unavailable" and /cost-report multiplied
all accumulated tokens by today's catalog price, so a price change rewrote
the past. Tariff versions are append-only and never backdated; a receipt
copies the version it was priced by, and zero comes only from ``free``.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.llm_tariffs import TariffRejected, add_tariff_version, tariff_in_force
from app.domain.work_budget_ledger import (
    initialize_budget_ledger,
    reserve_budget_for_dispatch,
    settle_llm_call_with_usage_receipt,
)
from app.domain.work_budget_usage import (
    TariffTerms,
    canonical_usage_payload,
    openai_usage_from_body,
    unknown_ollama_usage,
    valid_usage_receipt_payload,
)
from app.domain.work_orders import create_work_order


def _reservation():
    return SimpleNamespace(
        id=uuid.uuid4(),
        ledger_id=uuid.uuid4(),
        work_order_id=uuid.uuid4(),
        operation_key="llm:x",
        request_digest="r" * 64,
        binding_digest="b" * 64,
    )


def _counted(prompt=1000, completion=500, model="m"):
    return openai_usage_from_body(
        {
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
            "choices": [{"finish_reason": "stop"}],
        },
        provider="anthropic",
        model=model,
    )


PAID = TariffTerms("v-paid", "USD", Decimal("3"), Decimal("15"), False)
FREE = TariffTerms("v-free", "USD", None, None, True)


def test_a_priced_call_costs_its_tokens_at_the_copied_tariff():
    reservation = _reservation()
    payload = canonical_usage_payload(reservation, _counted(), owner_key="o", tariff=PAID)
    # 1000 × 3 / 1M + 500 × 15 / 1M
    assert payload["cost"] == {
        "status": "known",
        "currency": "USD",
        "amount": "0.0105",
        "tariff_version": "v-paid",
        "free": False,
        "input_per_1m": "3",
        "output_per_1m": "15",
    }
    assert valid_usage_receipt_payload(payload, reservation, owner_key="o")


def test_a_forged_amount_or_price_fails_the_check():
    reservation = _reservation()
    payload = canonical_usage_payload(reservation, _counted(), owner_key="o", tariff=PAID)
    for key, value in (("amount", "0.0001"), ("input_per_1m", "1"), ("free", True)):
        forged = {**payload, "cost": {**payload["cost"], key: value}}
        assert not valid_usage_receipt_payload(forged, reservation, owner_key="o")


def test_unknown_tokens_leave_a_paid_call_unknown_never_zero():
    reservation = _reservation()
    evidence = unknown_ollama_usage("response_not_observed", provider="anthropic", model="m")
    payload = canonical_usage_payload(reservation, evidence, owner_key="o", tariff=PAID)
    assert payload["cost"] == {
        "status": "unknown",
        "currency": "USD",
        "reason": "tokens_unknown",
        "tariff_version": "v-paid",
    }
    assert valid_usage_receipt_payload(payload, reservation, owner_key="o")
    # Known counters cannot hide behind "tokens_unknown".
    known = canonical_usage_payload(reservation, _counted(), owner_key="o", tariff=PAID)
    forged = {**known, "cost": payload["cost"]}
    assert not valid_usage_receipt_payload(forged, reservation, owner_key="o")


def test_only_an_explicitly_free_tariff_is_zero():
    reservation = _reservation()
    evidence = unknown_ollama_usage("response_not_observed", provider="strata", model="m")
    payload = canonical_usage_payload(reservation, evidence, owner_key="o", tariff=FREE)
    assert payload["cost"]["status"] == "known" and payload["cost"]["amount"] == "0"
    assert valid_usage_receipt_payload(payload, reservation, owner_key="o")
    without = canonical_usage_payload(reservation, evidence, owner_key="o")
    assert without["cost"]["reason"] == "tariff_unavailable"


def test_v1_receipts_never_carry_a_tariff():
    reservation = _reservation()
    payload = canonical_usage_payload(
        reservation, unknown_ollama_usage("missing"), owner_key="o", tariff=FREE
    )
    assert payload["schema"] == "llm_usage_receipt.v1"
    assert payload["cost"]["reason"] == "tariff_unavailable"


def _factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


async def _now(db):
    return await db.scalar(select(func.now()))


@pytest.mark.asyncio
async def test_tariff_versions_are_never_backdated_and_need_both_prices(db_session):
    model = f"t4-{uuid.uuid4().hex[:8]}"
    now = await _now(db_session)
    common = {"provider": "anthropic", "model": model, "source": "прайс", "created_by": "t"}
    with pytest.raises(TariffRejected, match="задним числом"):
        await add_tariff_version(
            db_session,
            currency="USD",
            input_per_1m=Decimal(1),
            output_per_1m=Decimal(1),
            free=False,
            effective_from=now - timedelta(minutes=1),
            **common,
        )
    with pytest.raises(TariffRejected, match="обе цены"):
        await add_tariff_version(
            db_session,
            currency="USD",
            input_per_1m=Decimal(1),
            output_per_1m=None,
            free=False,
            effective_from=None,
            **common,
        )
    with pytest.raises(TariffRejected, match="бесплатного"):
        await add_tariff_version(
            db_session,
            currency="USD",
            input_per_1m=Decimal(1),
            output_per_1m=None,
            free=True,
            effective_from=None,
            **common,
        )


@pytest.mark.asyncio
async def test_the_version_in_force_at_the_call_is_the_one_found(db_session):
    model = f"t4-{uuid.uuid4().hex[:8]}"
    now = await _now(db_session)
    first = await add_tariff_version(
        db_session,
        provider="anthropic",
        model=model,
        currency="usd",
        input_per_1m=Decimal("3"),
        output_per_1m=Decimal("15"),
        free=False,
        source="прайс 2026-10",
        effective_from=now + timedelta(hours=1),
        created_by="t",
        now=now,
    )
    await add_tariff_version(
        db_session,
        provider="anthropic",
        model=model,
        currency="USD",
        input_per_1m=Decimal("5"),
        output_per_1m=Decimal("25"),
        free=False,
        source="прайс 2026-11",
        effective_from=now + timedelta(hours=3),
        created_by="t",
        now=now,
    )
    assert await tariff_in_force(db_session, provider="anthropic", model=model, at=now) is None
    terms = await tariff_in_force(
        db_session, provider="anthropic", model=model, at=now + timedelta(hours=2)
    )
    assert terms.version == str(first.id) and terms.currency == "USD"
    assert terms.input_per_1m == Decimal("3")
    assert await tariff_in_force(db_session, provider="anthropic", model=None, at=now) is None
    await db_session.rollback()


@pytest.mark.asyncio
async def test_settlement_prices_by_reservation_time_and_stays_idempotent(test_engine):
    factory = _factory(test_engine)
    model = f"t4-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        order = await create_work_order(
            db, owner_key="t4-owner", objective="price a call", budgets={"max_llm_calls": 3}
        )
        await initialize_budget_ledger(db, order.id)
        tariff = await add_tariff_version(
            db,
            provider="anthropic",
            model=model,
            currency="USD",
            input_per_1m=Decimal("3"),
            output_per_1m=Decimal("15"),
            free=False,
            source="прайс",
            effective_from=None,
            created_by="t",
        )
        await db.commit()
        order_id, tariff_id = order.id, str(tariff.id)
    reservation, created = await reserve_budget_for_dispatch(
        factory,
        work_order_id=order_id,
        operation_key="llm:t4",
        dimension="llm_calls",
        units=1,
        request_digest=hashlib.sha256(b"t4").hexdigest(),
    )
    assert created
    evidence = _counted(model=model)
    _, event = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key="llm:t4",
        expected_owner_key="t4-owner",
        evidence=evidence,
    )
    assert event.payload["cost"]["status"] == "known"
    assert event.payload["cost"]["tariff_version"] == tariff_id
    assert event.payload["cost"]["amount"] == "0.0105"

    # A later price does not reprice the recorded call, and the re-settle
    # still matches the stored receipt.
    async with factory() as db:
        await add_tariff_version(
            db,
            provider="anthropic",
            model=model,
            currency="USD",
            input_per_1m=Decimal("30"),
            output_per_1m=Decimal("150"),
            free=False,
            source="новый прайс",
            effective_from=None,
            created_by="t",
        )
        await db.commit()
    _, again = await settle_llm_call_with_usage_receipt(
        factory,
        work_order_id=order_id,
        operation_key="llm:t4",
        expected_owner_key="t4-owner",
        evidence=evidence,
    )
    assert again.id == event.id and again.payload == event.payload


@pytest.mark.asyncio
async def test_tariff_api_appends_versions_and_refuses_backdating(client):
    model = f"t4-{uuid.uuid4().hex[:8]}"
    body = {
        "provider": "anthropic",
        "model": model,
        "input_per_1m": "3",
        "output_per_1m": "15",
        "source": "прайс провайдера",
    }
    created = await client.post("/api/providers/tariffs", json=body)
    assert created.status_code == 201, created.text
    assert created.json()["currency"] == "USD" and created.json()["free"] is False
    backdated = await client.post(
        "/api/providers/tariffs", json={**body, "effective_from": "2020-01-01T00:00:00Z"}
    )
    assert backdated.status_code == 422 and "задним числом" in backdated.json()["detail"]
    empty = await client.post("/api/providers/tariffs", json={**body, "output_per_1m": None})
    assert empty.status_code == 422
    listed = await client.get("/api/providers/tariffs", params={"model": model})
    assert [row["id"] for row in listed.json()] == [created.json()["id"]]


@pytest.mark.asyncio
async def test_cost_report_sums_receipts_by_their_own_price(client, test_engine):
    await test_settlement_prices_by_reservation_time_and_stays_idempotent(test_engine)
    report = await client.get("/api/providers/cost-report")
    assert report.status_code == 200, report.text
    rows = [r for r in report.json()["receipts"] if (r["model"] or "").startswith("t4-")]
    priced = [r for r in rows if r["priced_calls"]]
    assert priced and all(Decimal(r["amount"]) > 0 for r in priced)
    # The later 10× price did not reach the recorded call.
    assert any(r["amount"] == "0.0105" for r in priced)
