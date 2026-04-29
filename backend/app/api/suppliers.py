"""Supplier API — skills: supplier.get, supplier.search, supplier.price_history,
supplier.check_requisites, supplier.trust_score, supplier.alerts"""

import uuid
from collections import defaultdict

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import Invoice, InvoiceLine, InvoiceStatus, Party, PartyRole, SupplierProfile
from app.domain.suppliers import (
    PartyOut,
    PriceHistoryItem,
    PriceHistoryPoint,
    RequisiteCheck,
    RequisiteCheckResponse,
    SupplierAlert,
    SupplierAlertsResponse,
    SupplierFullOut,
    SupplierPriceHistoryResponse,
    SupplierProfileOut,
    SupplierSearchRequest,
    SupplierSearchResponse,
    SupplierUpdate,
    TrustScoreBreakdown,
    TrustScoreResponse,
)
from app.audit.service import log_action

router = APIRouter()
logger = structlog.get_logger()


# ── supplier.get ───────────────────────────────────────────────────────────


@router.get("/{supplier_id}", response_model=SupplierFullOut)
async def get_supplier(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.get — Get supplier profile with aggregated stats."""
    result = await db.execute(
        select(Party)
        .where(Party.id == supplier_id)
        .options(selectinload(Party.profile))
    )
    party = result.scalar_one_or_none()
    if not party:
        raise HTTPException(404, "Supplier not found")

    # Aggregate recent stats
    inv_count = (await db.execute(
        select(func.count()).select_from(
            select(Invoice).where(Invoice.supplier_id == supplier_id).subquery()
        )
    )).scalar() or 0

    open_amount = (await db.execute(
        select(func.coalesce(func.sum(Invoice.total_amount), 0.0))
        .where(
            Invoice.supplier_id == supplier_id,
            Invoice.status.in_([InvoiceStatus.needs_review, InvoiceStatus.draft]),
        )
    )).scalar() or 0.0

    out = SupplierFullOut.model_validate(party)
    out.profile = SupplierProfileOut.model_validate(party.profile) if party.profile else None
    out.recent_invoices_count = inv_count
    out.open_invoices_amount = float(open_amount)
    return out


# ── supplier.search ────────────────────────────────────────────────────────


@router.post("/search", response_model=SupplierSearchResponse)
async def search_suppliers(
    payload: SupplierSearchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.search — Search suppliers by name, INN, or address."""
    q = payload.query
    query = select(Party).where(
        or_(
            Party.name.ilike(f"%{q}%"),
            Party.inn.ilike(f"%{q}%"),
            Party.address.ilike(f"%{q}%"),
            Party.contact_email.ilike(f"%{q}%"),
        )
    )

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(Party.name).limit(payload.limit)
    result = await db.execute(query)
    parties = result.scalars().all()

    return SupplierSearchResponse(results=parties, total=total)


# ── supplier.price_history ─────────────────────────────────────────────────


@router.get("/{supplier_id}/price-history", response_model=SupplierPriceHistoryResponse)
async def supplier_price_history(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.price_history — Get price history for all items from this supplier."""
    party = (await db.execute(select(Party).where(Party.id == supplier_id))).scalar_one_or_none()
    if not party:
        raise HTTPException(404, "Supplier not found")

    # Get all invoices from this supplier with lines
    result = await db.execute(
        select(Invoice)
        .where(Invoice.supplier_id == supplier_id)
        .options(selectinload(Invoice.lines))
        .order_by(Invoice.invoice_date.asc())
    )
    invoices = result.scalars().all()

    # Build price history by item description
    items_map: dict[str, list[PriceHistoryPoint]] = defaultdict(list)
    for inv in invoices:
        date_str = inv.invoice_date.strftime("%Y-%m-%d") if inv.invoice_date else inv.created_at.strftime("%Y-%m-%d")
        for line in inv.lines:
            if line.description and line.unit_price is not None:
                key = line.description.strip()
                items_map[key].append(PriceHistoryPoint(
                    date=date_str,
                    price=line.unit_price,
                    invoice_number=inv.invoice_number,
                    invoice_id=str(inv.id),
                ))

    items: list[PriceHistoryItem] = []
    for desc, points in items_map.items():
        prices = [p.price for p in points]
        avg = sum(prices) / len(prices) if prices else 0
        trend = "stable"
        if len(prices) >= 2:
            if prices[-1] > prices[-2] * 1.05:
                trend = "up"
            elif prices[-1] < prices[-2] * 0.95:
                trend = "down"

        items.append(PriceHistoryItem(
            description=desc,
            points=points,
            current_price=prices[-1] if prices else None,
            min_price=min(prices) if prices else None,
            max_price=max(prices) if prices else None,
            avg_price=round(avg, 2),
            trend=trend,
        ))

    return SupplierPriceHistoryResponse(
        supplier_id=party.id,
        supplier_name=party.name,
        items=items,
        total_items=len(items),
    )


# ── supplier.check_requisites ─────────────────────────────────────────────


@router.post("/{supplier_id}/check-requisites", response_model=RequisiteCheckResponse)
async def check_requisites(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.check_requisites — Validate supplier requisites."""
    party = (await db.execute(select(Party).where(Party.id == supplier_id))).scalar_one_or_none()
    if not party:
        raise HTTPException(404, "Supplier not found")

    checks: list[RequisiteCheck] = []

    # INN check
    if not party.inn:
        checks.append(RequisiteCheck(field="inn", status="missing", message="ИНН не указан"))
    elif len(party.inn) not in (10, 12):
        checks.append(RequisiteCheck(field="inn", status="error", message=f"ИНН неверной длины: {len(party.inn)}"))
    else:
        checks.append(RequisiteCheck(field="inn", status="ok"))

    # KPP check
    if not party.kpp:
        checks.append(RequisiteCheck(field="kpp", status="warning", message="КПП не указан"))
    elif len(party.kpp) != 9:
        checks.append(RequisiteCheck(field="kpp", status="error", message=f"КПП неверной длины: {len(party.kpp)}"))
    else:
        checks.append(RequisiteCheck(field="kpp", status="ok"))

    # Bank details
    if not party.bank_account:
        checks.append(RequisiteCheck(field="bank_account", status="warning", message="Р/с не указан"))
    elif len(party.bank_account) != 20:
        checks.append(RequisiteCheck(field="bank_account", status="error", message="Р/с неверной длины"))
    else:
        checks.append(RequisiteCheck(field="bank_account", status="ok"))

    if not party.bank_bik:
        checks.append(RequisiteCheck(field="bank_bik", status="warning", message="БИК не указан"))
    elif len(party.bank_bik) != 9:
        checks.append(RequisiteCheck(field="bank_bik", status="error", message="БИК неверной длины"))
    else:
        checks.append(RequisiteCheck(field="bank_bik", status="ok"))

    # Contact
    if not party.contact_email:
        checks.append(RequisiteCheck(field="contact_email", status="warning", message="Email не указан"))
    else:
        checks.append(RequisiteCheck(field="contact_email", status="ok"))

    if not party.address:
        checks.append(RequisiteCheck(field="address", status="warning", message="Адрес не указан"))
    else:
        checks.append(RequisiteCheck(field="address", status="ok"))

    is_valid = not any(c.status == "error" for c in checks)
    return RequisiteCheckResponse(supplier_id=party.id, is_valid=is_valid, checks=checks)


# ── supplier.trust_score ───────────────────────────────────────────────────


@router.get("/{supplier_id}/trust-score", response_model=TrustScoreResponse)
async def get_trust_score(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.trust_score — Calculate supplier trust score."""
    party = (await db.execute(
        select(Party).where(Party.id == supplier_id).options(selectinload(Party.profile))
    )).scalar_one_or_none()
    if not party:
        raise HTTPException(404, "Supplier not found")

    breakdown: list[TrustScoreBreakdown] = []

    # Factor 1: Requisites completeness (weight 0.2)
    req_fields = [party.inn, party.kpp, party.bank_account, party.bank_bik, party.address, party.contact_email]
    filled = sum(1 for f in req_fields if f)
    req_score = filled / len(req_fields)
    breakdown.append(TrustScoreBreakdown(
        factor="requisites", weight=0.2, score=req_score,
        detail=f"{filled}/{len(req_fields)} реквизитов заполнено",
    ))

    # Factor 2: Invoice history (weight 0.3)
    inv_count = (await db.execute(
        select(func.count()).select_from(
            select(Invoice).where(Invoice.supplier_id == supplier_id).subquery()
        )
    )).scalar() or 0
    hist_score = min(inv_count / 10, 1.0)  # max at 10+ invoices
    breakdown.append(TrustScoreBreakdown(
        factor="invoice_history", weight=0.3, score=hist_score,
        detail=f"{inv_count} счетов в истории",
    ))

    # Factor 3: Approval rate (weight 0.3)
    approved = (await db.execute(
        select(func.count()).select_from(
            select(Invoice).where(
                Invoice.supplier_id == supplier_id,
                Invoice.status == InvoiceStatus.approved,
            ).subquery()
        )
    )).scalar() or 0
    approval_rate = approved / inv_count if inv_count > 0 else 0.5
    breakdown.append(TrustScoreBreakdown(
        factor="approval_rate", weight=0.3, score=approval_rate,
        detail=f"{approved}/{inv_count} утверждено",
    ))

    # Factor 4: Price stability (weight 0.2)
    # Check if recent prices deviate significantly
    price_score = 0.8  # default good
    result = await db.execute(
        select(Invoice)
        .where(Invoice.supplier_id == supplier_id)
        .options(selectinload(Invoice.lines))
        .order_by(Invoice.created_at.desc())
        .limit(5)
    )
    recent = result.scalars().all()
    if len(recent) >= 2:
        # Compare total amounts
        amounts = [inv.total_amount for inv in recent if inv.total_amount]
        if len(amounts) >= 2:
            avg_amount = sum(amounts) / len(amounts)
            max_dev = max(abs(a - avg_amount) / avg_amount for a in amounts) if avg_amount > 0 else 0
            price_score = max(0, 1.0 - max_dev)

    breakdown.append(TrustScoreBreakdown(
        factor="price_stability", weight=0.2, score=round(price_score, 2),
        detail=f"Стабильность цен за последние {len(recent)} счетов",
    ))

    # Calculate weighted total
    total_score = sum(b.weight * b.score for b in breakdown)
    total_score = round(total_score, 2)

    recommendation = None
    if total_score >= 0.8:
        recommendation = "Надёжный поставщик"
    elif total_score >= 0.5:
        recommendation = "Требует внимания — проверьте реквизиты и историю"
    else:
        recommendation = "Высокий риск — рекомендуется дополнительная проверка"

    # Save to profile
    if party.profile:
        party.profile.trust_score = total_score
        await db.commit()

    return TrustScoreResponse(
        supplier_id=party.id,
        trust_score=total_score,
        breakdown=breakdown,
        recommendation=recommendation,
    )


# ── supplier.alerts ────────────────────────────────────────────────────────


@router.get("/{supplier_id}/alerts", response_model=SupplierAlertsResponse)
async def supplier_alerts(
    supplier_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.alerts — Get alerts for a supplier."""
    party = (await db.execute(select(Party).where(Party.id == supplier_id))).scalar_one_or_none()
    if not party:
        raise HTTPException(404, "Supplier not found")

    alerts: list[SupplierAlert] = []
    alert_id = 0

    # Alert: missing requisites
    if not party.inn:
        alert_id += 1
        alerts.append(SupplierAlert(
            id=str(alert_id), alert_type="missing_docs", severity="warning",
            message="ИНН не указан", created_at="",
        ))
    if not party.bank_account:
        alert_id += 1
        alerts.append(SupplierAlert(
            id=str(alert_id), alert_type="missing_docs", severity="warning",
            message="Банковские реквизиты не заполнены", created_at="",
        ))

    # Alert: price increases
    result = await db.execute(
        select(Invoice)
        .where(Invoice.supplier_id == supplier_id)
        .options(selectinload(Invoice.lines))
        .order_by(Invoice.created_at.desc())
        .limit(2)
    )
    recent_invs = result.scalars().all()
    if len(recent_invs) >= 2:
        new_total = recent_invs[0].total_amount or 0
        old_total = recent_invs[1].total_amount or 0
        if old_total > 0 and new_total > old_total * 1.15:
            pct = round((new_total - old_total) / old_total * 100, 1)
            alert_id += 1
            alerts.append(SupplierAlert(
                id=str(alert_id), alert_type="price_increase", severity="warning",
                message=f"Рост суммы счёта на {pct}% (с {old_total:.0f} до {new_total:.0f})",
                created_at=recent_invs[0].created_at.isoformat() if recent_invs[0].created_at else "",
                entity_id=str(recent_invs[0].id),
            ))

    # Alert: overdue (needs_review for too long)
    from datetime import datetime, timedelta, timezone
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    stale_count = (await db.execute(
        select(func.count()).select_from(
            select(Invoice).where(
                Invoice.supplier_id == supplier_id,
                Invoice.status == InvoiceStatus.needs_review,
                Invoice.created_at < stale_cutoff,
            ).subquery()
        )
    )).scalar() or 0
    if stale_count > 0:
        alert_id += 1
        alerts.append(SupplierAlert(
            id=str(alert_id), alert_type="overdue", severity="error",
            message=f"{stale_count} счетов на проверке более 7 дней",
            created_at="",
        ))

    return SupplierAlertsResponse(supplier_id=party.id, alerts=alerts, total=len(alerts))


# ── supplier.update ────────────────────────────────────────────────────────


@router.patch("/{supplier_id}", response_model=PartyOut)
async def update_supplier(
    supplier_id: uuid.UUID,
    payload: SupplierUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.update — Update supplier details."""
    party = (await db.execute(select(Party).where(Party.id == supplier_id))).scalar_one_or_none()
    if not party:
        raise HTTPException(404, "Supplier not found")

    update_data = payload.model_dump(exclude_unset=True)

    # Notes go to profile
    notes = update_data.pop("notes", None)

    for field, value in update_data.items():
        setattr(party, field, value)

    if notes is not None and party.profile:
        party.profile.notes = notes

    await log_action(
        db, action="supplier.update", entity_type="supplier",
        entity_id=party.id, details=update_data,
    )
    await db.commit()
    await db.refresh(party)
    return party


# ── List suppliers ─────────────────────────────────────────────────────────


@router.get("", response_model=list[PartyOut])
async def list_suppliers(
    role: str | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Skill: supplier.list — List all suppliers/parties."""
    query = select(Party)
    if role:
        try:
            query = query.where(Party.role == PartyRole(role))
        except ValueError:
            pass
    query = query.order_by(Party.name).offset(offset).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()
