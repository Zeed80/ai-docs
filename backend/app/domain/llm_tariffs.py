"""T4: versioned LLM tariffs.

A tariff version is appended, never edited, and never takes effect before it
was written — so the version a receipt found when its call was settled is the
one any later re-check finds too, and a new price cannot rewrite past costs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.db.work_budget_models import LlmTariff
from app.domain.work_budget_usage import RECEIPT_PROVIDERS, TariffTerms


class TariffRejected(ValueError):
    """The version cannot be recorded as asked."""


def terms_of(row: LlmTariff) -> TariffTerms:
    return TariffTerms(
        version=str(row.id),
        currency=row.currency,
        input_per_1m=None if row.free else Decimal(row.input_per_1m),
        output_per_1m=None if row.free else Decimal(row.output_per_1m),
        free=row.free,
    )


async def tariff_in_force(
    db, *, provider: str, model: str | None, at: datetime | None
) -> TariffTerms | None:
    """The latest version effective at ``at``; None without a model or a version."""
    if not model or at is None:
        return None
    row = await db.scalar(
        select(LlmTariff)
        .where(
            LlmTariff.provider == provider,
            LlmTariff.model == model,
            LlmTariff.effective_from <= at,
        )
        .order_by(LlmTariff.effective_from.desc())
        .limit(1)
    )
    return terms_of(row) if row is not None else None


async def add_tariff_version(
    db,
    *,
    provider: str,
    model: str,
    currency: str,
    input_per_1m: Decimal | None,
    output_per_1m: Decimal | None,
    free: bool,
    source: str,
    effective_from: datetime | None,
    created_by: str | None,
    now: datetime | None = None,
) -> LlmTariff:
    # The database clock: reservations take ``reserved_at`` from it too.
    now = now or await db.scalar(select(func.now()))
    effective = effective_from or now
    if effective.tzinfo is None:
        raise TariffRejected("Дата начала действия должна быть с часовым поясом")
    if effective < now:
        raise TariffRejected(
            "Тариф не вводится задним числом: прошлые вызовы уже посчитаны по своей цене"
        )
    if provider not in RECEIPT_PROVIDERS:
        raise TariffRejected(f"Неизвестный провайдер: {provider}")
    currency = currency.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise TariffRejected("Валюта — трёхбуквенный код (USD, RUB)")
    if not source.strip():
        raise TariffRejected("Нужен источник цены")
    if free:
        if input_per_1m or output_per_1m:
            raise TariffRejected("У бесплатного тарифа нет цены")
        input_per_1m = output_per_1m = None
    elif input_per_1m is None or output_per_1m is None:
        raise TariffRejected(
            "Нужны обе цены; ноль ставится только флагом «бесплатно», а не пустой ценой"
        )
    elif input_per_1m < 0 or output_per_1m < 0:
        raise TariffRejected("Цена не может быть отрицательной")
    elif input_per_1m == 0 and output_per_1m == 0:
        # A zero price is a claim the call costs nothing; it has its own flag
        # so "free" is a decision, not a placeholder (live smoke 2026-10-12).
        raise TariffRejected("Обе цены нулевые — это тариф «бесплатно», отметьте его флагом")
    row = LlmTariff(
        id=uuid.uuid4(),
        provider=provider,
        model=model.strip(),
        currency=currency,
        input_per_1m=input_per_1m,
        output_per_1m=output_per_1m,
        free=free,
        source=source.strip(),
        effective_from=effective,
        created_by=created_by,
    )
    db.add(row)
    await db.flush()
    return row
