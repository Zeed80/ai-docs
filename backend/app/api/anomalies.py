"""Anomaly Detection API — skills: anomaly.check_all, anomaly.create_card,
anomaly.resolve, anomaly.explain"""

import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.db.models import (
    AnomalyCard,
    AnomalySeverity,
    AnomalyStatus,
    AnomalyType,
    Document,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Party,
)
from app.domain.anomalies import (
    AnomalyCardOut,
    AnomalyCheckRequest,
    AnomalyCheckResponse,
    AnomalyCreateRequest,
    AnomalyExplainResponse,
    AnomalyResolveRequest,
)
from app.audit.service import log_action, add_timeline_event

router = APIRouter()
logger = structlog.get_logger()


# ── anomaly.check_all ──────────────────────────────────────────────────────


@router.post("/check", response_model=AnomalyCheckResponse)
async def check_all_anomalies(
    payload: AnomalyCheckRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: anomaly.check_all — Run all anomaly detectors on an invoice."""
    if not payload.invoice_id:
        raise HTTPException(400, "invoice_id required")

    result = await db.execute(
        select(Invoice)
        .where(Invoice.id == payload.invoice_id)
        .options(selectinload(Invoice.lines), selectinload(Invoice.supplier))
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    anomalies: list[AnomalyCard] = []

    # Detector 1: Duplicate invoice (same number + supplier)
    dup = await _detect_duplicate(db, invoice)
    if dup:
        anomalies.append(dup)

    # Detector 2: New supplier (first invoice from this supplier)
    new_sup = await _detect_new_supplier(db, invoice)
    if new_sup:
        anomalies.append(new_sup)

    # Detector 3: Requisite change
    req_change = await _detect_requisite_change(db, invoice)
    if req_change:
        anomalies.append(req_change)

    # Detector 4: Price spike (>20% increase)
    price_spike = await _detect_price_spike(db, invoice)
    if price_spike:
        anomalies.append(price_spike)

    # Detector 5: Unknown items (not in canonical catalog)
    unknown = await _detect_unknown_items(db, invoice)
    if unknown:
        anomalies.append(unknown)

    # Persist anomalies
    for a in anomalies:
        db.add(a)

    if anomalies:
        await db.commit()
        for a in anomalies:
            await db.refresh(a)

    logger.info(
        "anomaly_check_complete",
        invoice_id=str(invoice.id),
        found=len(anomalies),
    )

    return AnomalyCheckResponse(
        entity_id=invoice.id,
        anomalies_found=len(anomalies),
        anomalies=anomalies,
    )


async def _detect_duplicate(db: AsyncSession, invoice: Invoice) -> AnomalyCard | None:
    """Detect duplicate invoice: same number + same supplier."""
    if not invoice.invoice_number or not invoice.supplier_id:
        return None

    result = await db.execute(
        select(func.count()).select_from(
            select(Invoice).where(
                Invoice.invoice_number == invoice.invoice_number,
                Invoice.supplier_id == invoice.supplier_id,
                Invoice.id != invoice.id,
            ).subquery()
        )
    )
    count = result.scalar() or 0
    if count > 0:
        return AnomalyCard(
            anomaly_type=AnomalyType.duplicate,
            severity=AnomalySeverity.critical,
            entity_type="invoice",
            entity_id=invoice.id,
            title=f"Дубликат счёта {invoice.invoice_number}",
            description=f"Найдено {count} других счетов с таким же номером от этого поставщика",
            details={"duplicate_count": count, "invoice_number": invoice.invoice_number},
        )
    return None


async def _detect_new_supplier(db: AsyncSession, invoice: Invoice) -> AnomalyCard | None:
    """Detect first invoice from a supplier."""
    if not invoice.supplier_id:
        return None

    result = await db.execute(
        select(func.count()).select_from(
            select(Invoice).where(
                Invoice.supplier_id == invoice.supplier_id,
                Invoice.id != invoice.id,
            ).subquery()
        )
    )
    count = result.scalar() or 0
    if count == 0:
        supplier_name = invoice.supplier.name if invoice.supplier else "неизвестен"
        return AnomalyCard(
            anomaly_type=AnomalyType.new_supplier,
            severity=AnomalySeverity.info,
            entity_type="invoice",
            entity_id=invoice.id,
            title=f"Новый поставщик: {supplier_name}",
            description="Первый счёт от этого поставщика — рекомендуется проверить реквизиты",
            details={"supplier_name": supplier_name, "supplier_id": str(invoice.supplier_id)},
        )
    return None


async def _detect_requisite_change(db: AsyncSession, invoice: Invoice) -> AnomalyCard | None:
    """Detect if supplier requisites differ from previous invoices."""
    if not invoice.supplier_id or not invoice.supplier:
        return None

    # Get the previous invoice's extraction to compare bank details
    prev_result = await db.execute(
        select(Invoice)
        .where(
            Invoice.supplier_id == invoice.supplier_id,
            Invoice.id != invoice.id,
        )
        .order_by(Invoice.created_at.desc())
        .limit(1)
    )
    prev_inv = prev_result.scalar_one_or_none()
    if not prev_inv:
        return None

    # Compare stored metadata if available
    curr_meta = invoice.metadata_ or {}
    prev_meta = prev_inv.metadata_ or {}

    changed_fields = []
    for key in ("supplier_bank_account", "supplier_bik", "supplier_inn"):
        if curr_meta.get(key) and prev_meta.get(key) and curr_meta[key] != prev_meta[key]:
            changed_fields.append(key)

    if changed_fields:
        return AnomalyCard(
            anomaly_type=AnomalyType.requisite_change,
            severity=AnomalySeverity.critical,
            entity_type="invoice",
            entity_id=invoice.id,
            title="Смена реквизитов поставщика",
            description=f"Изменились поля: {', '.join(changed_fields)}",
            details={"changed_fields": changed_fields},
        )
    return None


async def _detect_price_spike(db: AsyncSession, invoice: Invoice) -> AnomalyCard | None:
    """Detect >20% price increase on any line item."""
    if not invoice.supplier_id or not invoice.lines:
        return None

    # Get previous invoice lines
    prev_result = await db.execute(
        select(Invoice)
        .where(
            Invoice.supplier_id == invoice.supplier_id,
            Invoice.id != invoice.id,
        )
        .options(selectinload(Invoice.lines))
        .order_by(Invoice.created_at.desc())
        .limit(3)
    )
    prev_invoices = prev_result.scalars().all()
    if not prev_invoices:
        return None

    # Build price map from previous invoices
    prev_prices: dict[str, float] = {}
    for pi in prev_invoices:
        for line in pi.lines:
            if line.description and line.unit_price is not None:
                key = line.description.lower().strip()
                if key not in prev_prices:
                    prev_prices[key] = line.unit_price

    # Check current lines
    spikes = []
    for line in invoice.lines:
        if not line.description or line.unit_price is None:
            continue
        key = line.description.lower().strip()
        prev_price = prev_prices.get(key)
        if prev_price and prev_price > 0:
            change_pct = (line.unit_price - prev_price) / prev_price * 100
            if change_pct > 20:
                spikes.append({
                    "item": line.description,
                    "old_price": prev_price,
                    "new_price": line.unit_price,
                    "change_pct": round(change_pct, 1),
                })

    if spikes:
        worst = max(spikes, key=lambda s: s["change_pct"])
        return AnomalyCard(
            anomaly_type=AnomalyType.price_spike,
            severity=AnomalySeverity.warning,
            entity_type="invoice",
            entity_id=invoice.id,
            title=f"Скачок цены: {worst['item']} (+{worst['change_pct']}%)",
            description=f"{len(spikes)} позиций с ростом >20%",
            details={"spikes": spikes},
        )
    return None


async def _detect_unknown_items(db: AsyncSession, invoice: Invoice) -> AnomalyCard | None:
    """Detect line items without canonical_item_id mapping."""
    if not invoice.lines:
        return None

    unmapped = [
        line.description
        for line in invoice.lines
        if line.description and not line.canonical_item_id
    ]

    if len(unmapped) > 0 and len(unmapped) == len(invoice.lines):
        return AnomalyCard(
            anomaly_type=AnomalyType.unknown_item,
            severity=AnomalySeverity.info,
            entity_type="invoice",
            entity_id=invoice.id,
            title=f"Нет в справочнике: {len(unmapped)} позиций",
            description="Все позиции не привязаны к каноническому справочнику",
            details={"unmapped_items": unmapped[:10]},
        )
    return None


# ── anomaly.create_card ────────────────────────────────────────────────────


@router.post("", response_model=AnomalyCardOut)
async def create_anomaly(
    payload: AnomalyCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: anomaly.create_card — Manually create an anomaly card."""
    card = AnomalyCard(
        anomaly_type=AnomalyType(payload.anomaly_type),
        severity=AnomalySeverity(payload.severity),
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        title=payload.title,
        description=payload.description,
        details=payload.details,
    )
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


# ── List anomalies ─────────────────────────────────────────────────────────


@router.get("", response_model=list[AnomalyCardOut])
async def list_anomalies(
    status: str | None = None,
    entity_id: uuid.UUID | None = None,
    severity: str | None = None,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Skill: anomaly.list — List anomaly cards."""
    query = select(AnomalyCard)
    if status:
        try:
            query = query.where(AnomalyCard.status == AnomalyStatus(status))
        except ValueError:
            pass
    if entity_id:
        query = query.where(AnomalyCard.entity_id == entity_id)
    if severity:
        try:
            query = query.where(AnomalyCard.severity == AnomalySeverity(severity))
        except ValueError:
            pass
    query = query.order_by(AnomalyCard.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


# ── anomaly.resolve ────────────────────────────────────────────────────────


@router.post("/{anomaly_id}/resolve", response_model=AnomalyCardOut)
async def resolve_anomaly(
    anomaly_id: uuid.UUID,
    payload: AnomalyResolveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Skill: anomaly.resolve — Resolve an anomaly (approval gate)."""
    result = await db.execute(
        select(AnomalyCard).where(AnomalyCard.id == anomaly_id)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(404, "Anomaly not found")
    if card.status != AnomalyStatus.open:
        raise HTTPException(400, f"Cannot resolve anomaly in status {card.status.value}")

    if payload.resolution == "false_positive":
        card.status = AnomalyStatus.false_positive
    else:
        card.status = AnomalyStatus.resolved

    card.resolved_by = "user"
    card.resolved_at = datetime.now(timezone.utc)
    card.resolution_comment = payload.comment

    await log_action(
        db, action="anomaly.resolve", entity_type="anomaly",
        entity_id=card.id,
        details={"resolution": payload.resolution, "comment": payload.comment},
    )
    await add_timeline_event(
        db, entity_type=card.entity_type, entity_id=card.entity_id,
        event_type="anomaly_resolved",
        summary=f"Аномалия «{card.title}» — {payload.resolution}",
        actor="user",
    )
    await db.commit()
    await db.refresh(card)
    return card


# ── anomaly.explain ────────────────────────────────────────────────────────


EXPLAIN_MAP = {
    "duplicate": {
        "explanation": "Обнаружен счёт с тем же номером от того же поставщика. Это может быть повторная отправка, ошибка нумерации или попытка двойной оплаты.",
        "actions": [
            "Проверить оба счёта на совпадение сумм и позиций",
            "Связаться с поставщиком для уточнения",
            "Отметить как ложное срабатывание, если это допустимая пересылка",
        ],
    },
    "new_supplier": {
        "explanation": "Это первый счёт от данного поставщика в системе. Новые поставщики требуют дополнительной проверки реквизитов.",
        "actions": [
            "Проверить реквизиты поставщика (ИНН, КПП, банк)",
            "Убедиться в наличии договора",
            "Проверить адрес и контакты",
        ],
    },
    "requisite_change": {
        "explanation": "Банковские реквизиты поставщика отличаются от предыдущих счетов. Это может быть легитимная смена банка или попытка мошенничества.",
        "actions": [
            "Связаться с поставщиком по ИЗВЕСТНОМУ телефону (не из счёта)",
            "Запросить письменное уведомление о смене реквизитов",
            "Не оплачивать до подтверждения",
        ],
    },
    "price_spike": {
        "explanation": "Цена одной или нескольких позиций выросла более чем на 20% по сравнению с предыдущими счетами.",
        "actions": [
            "Сравнить цены с рынком",
            "Запросить обоснование повышения",
            "Рассмотреть альтернативных поставщиков",
        ],
    },
    "invoice_email_mismatch": {
        "explanation": "Данные в счёте не совпадают с информацией из сопроводительного письма.",
        "actions": [
            "Сверить суммы, даты и номера",
            "Запросить корректировку у поставщика",
        ],
    },
    "unknown_item": {
        "explanation": "Позиции счёта не найдены в каноническом справочнике товаров. Это может быть новая номенклатура или нестандартное описание.",
        "actions": [
            "Проверить описания и привязать к справочнику",
            "Создать новые позиции в справочнике при необходимости",
        ],
    },
}


@router.get("/{anomaly_id}/explain", response_model=AnomalyExplainResponse)
async def explain_anomaly(
    anomaly_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Skill: anomaly.explain — Get human-readable explanation of an anomaly."""
    result = await db.execute(
        select(AnomalyCard).where(AnomalyCard.id == anomaly_id)
    )
    card = result.scalar_one_or_none()
    if not card:
        raise HTTPException(404, "Anomaly not found")

    info = EXPLAIN_MAP.get(card.anomaly_type.value, {
        "explanation": card.description or "Неизвестный тип аномалии",
        "actions": ["Проверить вручную"],
    })

    return AnomalyExplainResponse(
        anomaly_id=card.id,
        anomaly_type=card.anomaly_type.value,
        title=card.title,
        explanation=info["explanation"],
        suggested_actions=info["actions"],
        context=card.details or {},
    )
