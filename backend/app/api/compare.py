"""Compare КП API — skills: compare.create, compare.align, compare.decide,
compare.summary"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import CompareSession, Invoice, InvoiceLine
from app.domain.compare import (
    AlignedItem,
    CompareAlignResponse,
    CompareCreateRequest,
    CompareDecideRequest,
    CompareSessionOut,
    CompareSummaryResponse,
)
from app.audit.service import log_action

router = APIRouter()
logger = structlog.get_logger()


# ── compare.create ────────────────────────────────────────────────────────


@router.post("", response_model=CompareSessionOut)
async def create_session(
    payload: CompareCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: compare.create — Create a comparison session for commercial offers."""
    if len(payload.invoice_ids) < 2:
        raise HTTPException(400, "At least 2 invoices required for comparison")

    # Validate invoices exist
    result = await db.execute(
        select(Invoice).where(Invoice.id.in_(payload.invoice_ids))
    )
    invoices = result.scalars().all()
    if len(invoices) != len(payload.invoice_ids):
        raise HTTPException(404, "Some invoices not found")

    session = CompareSession(
        name=payload.name,
        status="draft",
        invoice_ids=[str(iid) for iid in payload.invoice_ids],
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    logger.info("compare_session_created", session_id=str(session.id), invoices=len(invoices))
    return session


# ── compare.list ──────────────────────────────────────────────────────────


@router.get("", response_model=list[CompareSessionOut])
async def list_sessions(
    status: str | None = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: compare.list — List comparison sessions."""
    query = select(CompareSession)
    if status:
        query = query.where(CompareSession.status == status)
    query = query.order_by(CompareSession.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


# ── compare.get ───────────────────────────────────────────────────────────


@router.get("/{session_id}", response_model=CompareSessionOut)
async def get_session(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: compare.get — Get a comparison session."""
    result = await db.execute(
        select(CompareSession).where(CompareSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    return session


# ── compare.align ─────────────────────────────────────────────────────────


@router.post("/{session_id}/align", response_model=CompareAlignResponse)
async def align_items(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: compare.align — Align line items across invoices for comparison."""
    result = await db.execute(
        select(CompareSession).where(CompareSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    invoice_ids = [uuid.UUID(iid) for iid in session.invoice_ids]

    # Load invoices with lines and supplier
    inv_result = await db.execute(
        select(Invoice)
        .where(Invoice.id.in_(invoice_ids))
        .options(selectinload(Invoice.lines), selectinload(Invoice.supplier))
    )
    invoices = inv_result.scalars().all()

    # Build alignment: group lines by normalized description
    alignment_map: dict[str, dict[str, dict]] = {}

    for inv in invoices:
        supplier_key = str(inv.supplier_id) if inv.supplier_id else str(inv.id)
        for line in inv.lines:
            if not line.description:
                continue
            key = line.description.lower().strip()

            if key not in alignment_map:
                alignment_map[key] = {}

            alignment_map[key][supplier_key] = {
                "description": line.description,
                "quantity": line.quantity,
                "unit": line.unit,
                "unit_price": float(line.unit_price) if line.unit_price else None,
                "amount": float(line.amount) if line.amount else None,
            }

    aligned_items = []
    for canonical, items in alignment_map.items():
        aligned_items.append(AlignedItem(
            canonical_name=canonical,
            items=items,
        ))

    # Save alignment to session
    session.alignment = {
        "items": [item.model_dump() for item in aligned_items],
        "suppliers": _build_supplier_map(invoices),
    }
    session.status = "aligned"
    await db.commit()
    await db.refresh(session)

    return CompareAlignResponse(
        session_id=session.id,
        items=aligned_items,
    )


def _build_supplier_map(invoices: list[Invoice]) -> dict:
    """Build supplier_id → {name, invoice_number, total} map."""
    result = {}
    for inv in invoices:
        key = str(inv.supplier_id) if inv.supplier_id else str(inv.id)
        result[key] = {
            "name": inv.supplier.name if inv.supplier else f"Invoice {inv.invoice_number}",
            "invoice_number": inv.invoice_number,
            "total_amount": float(inv.total_amount) if inv.total_amount else None,
        }
    return result


# ── compare.decide ────────────────────────────────────────────────────────


@router.post("/{session_id}/decide", response_model=CompareSessionOut)
async def decide(
    session_id: uuid.UUID,
    payload: CompareDecideRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: compare.decide — Choose a supplier (approval gate)."""
    result = await db.execute(
        select(CompareSession).where(CompareSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")
    if session.status == "decided":
        raise HTTPException(400, "Session already decided")

    session.decision = {
        "chosen_supplier_id": str(payload.chosen_supplier_id),
        "reasoning": payload.reasoning,
    }
    session.status = "decided"
    session.decided_by = "user"
    session.decided_at = datetime.now(timezone.utc)

    await log_action(
        db, action="compare.decide", entity_type="compare_session",
        entity_id=session.id,
        details={
            "chosen_supplier_id": str(payload.chosen_supplier_id),
            "reasoning": payload.reasoning,
        },
    )
    await db.commit()
    await db.refresh(session)

    logger.info("compare_decided", session_id=str(session.id))
    return session


# ── compare.summary ───────────────────────────────────────────────────────


@router.get("/{session_id}/summary", response_model=CompareSummaryResponse)
async def summary(
    session_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: compare.summary — Get comparison summary with recommendation."""
    result = await db.execute(
        select(CompareSession).where(CompareSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(404, "Session not found")

    if not session.alignment:
        raise HTTPException(400, "Session not aligned yet — call /align first")

    alignment = session.alignment
    items = alignment.get("items", [])
    supplier_map = alignment.get("suppliers", {})

    # Calculate totals per supplier
    supplier_totals: dict[str, float] = {}
    for item in items:
        for sup_id, data in item.get("items", {}).items():
            if data and data.get("amount"):
                supplier_totals[sup_id] = supplier_totals.get(sup_id, 0) + data["amount"]

    suppliers_list = []
    for sup_id, total in supplier_totals.items():
        info = supplier_map.get(sup_id, {})
        suppliers_list.append({
            "supplier_id": sup_id,
            "name": info.get("name", "Unknown"),
            "invoice_number": info.get("invoice_number"),
            "total": round(total, 2),
            "invoice_total": info.get("total_amount"),
        })

    # Sort by total ascending
    suppliers_list.sort(key=lambda s: s["total"])

    cheapest = suppliers_list[0] if suppliers_list else None
    recommendation = None
    if cheapest and len(suppliers_list) > 1:
        savings = suppliers_list[-1]["total"] - cheapest["total"]
        recommendation = (
            f"Рекомендация: {cheapest['name']} — наименьшая сумма "
            f"({cheapest['total']:,.0f} ₽), экономия {savings:,.0f} ₽ "
            f"по сравнению с самым дорогим предложением"
        )

    return CompareSummaryResponse(
        session_id=session.id,
        total_items=len(items),
        suppliers=suppliers_list,
        cheapest_total=cheapest,
        recommendation=recommendation,
    )
