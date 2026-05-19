"""Seed data script — populate DB with test suppliers, documents, invoices.

Usage: python -m app.scripts.seed_data
"""

import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.db.base import Base
from app.db.models import (
    Approval,
    ApprovalActionType,
    ApprovalStatus,
    AnomalyCard,
    AnomalyStatus,
    AnomalyType,
    CalendarEvent,
    CanonicalItem,
    CompareSession,
    Document,
    DocumentStatus,
    DocumentType,
    EmailMessage,
    EmailThread,
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Party,
    PartyRole,
    PriceHistoryEntry,
    Reminder,
    SupplierProfile,
)


def seed():
    engine = create_engine(settings.database_url_sync)
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        # Check if already seeded
        existing = db.query(Party).first()
        if existing:
            print("Database already seeded, skipping.")
            return

        now = datetime.now(timezone.utc)

        # ── Parties (Suppliers) ──────────────────────────────────────────

        suppliers = [
            Party(
                name='ООО "АКМЕ Поставки"',
                inn="7701234567",
                kpp="770101001",
                ogrn="1027700123456",
                address="г. Москва, ул. Промышленная, д. 15",
                role=PartyRole.supplier,
                bank_name="ПАО Сбербанк",
                bank_bik="044525225",
                bank_account="40702810938000012345",
                corr_account="30101810400000000225",
                contact_email="sales@acme-supply.ru",
                contact_phone="+7 (495) 123-45-67",
            ),
            Party(
                name='АО "ТехноКомплект"',
                inn="7702345678",
                kpp="770201001",
                address="г. Москва, ул. Заводская, д. 8",
                role=PartyRole.supplier,
                bank_name="ПАО ВТБ",
                bank_bik="044525187",
                bank_account="40702810700000054321",
                corr_account="30101810700000000187",
                contact_email="info@technokomplekt.ru",
                contact_phone="+7 (495) 234-56-78",
            ),
            Party(
                name='ИП Сидоров А.В.',
                inn="772012345678",
                address="г. Москва, ул. Мастеровая, д. 3",
                role=PartyRole.supplier,
                contact_email="sidorov@mail.ru",
                contact_phone="+7 (926) 345-67-89",
            ),
        ]

        buyer = Party(
            name='ООО "Наш Завод"',
            inn="7703456789",
            kpp="770301001",
            address="г. Москва, ул. Производственная, д. 1",
            role=PartyRole.buyer,
            contact_email="procurement@nashzavod.ru",
        )

        db.add_all(suppliers)
        db.add(buyer)
        db.flush()

        # ── Supplier Profiles ────────────────────────────────────────────

        profiles = [
            SupplierProfile(
                party_id=suppliers[0].id,
                total_invoices=47,
                total_amount=3_850_000.00,
                avg_processing_days=3.2,
                last_invoice_date=now - timedelta(days=5),
                trust_score=0.92,
                notes="Основной поставщик метизов. Стабильные цены.",
            ),
            SupplierProfile(
                party_id=suppliers[1].id,
                total_invoices=12,
                total_amount=1_200_000.00,
                avg_processing_days=5.1,
                last_invoice_date=now - timedelta(days=15),
                trust_score=0.78,
            ),
            SupplierProfile(
                party_id=suppliers[2].id,
                total_invoices=3,
                total_amount=180_000.00,
                avg_processing_days=2.0,
                last_invoice_date=now - timedelta(days=45),
                trust_score=0.65,
                notes="Новый поставщик, требует проверки.",
            ),
        ]
        db.add_all(profiles)

        # ── Email Threads ────────────────────────────────────────────────

        thread1 = EmailThread(
            subject="Счёт №123 от АКМЕ за март",
            mailbox="procurement",
            party_id=suppliers[0].id,
            message_count=3,
            last_message_at=now - timedelta(hours=2),
        )
        thread2 = EmailThread(
            subject="КП на подшипники SKF",
            mailbox="procurement",
            party_id=suppliers[1].id,
            message_count=1,
            last_message_at=now - timedelta(days=1),
        )
        db.add_all([thread1, thread2])
        db.flush()

        # ── Email Messages ───────────────────────────────────────────────

        email1 = EmailMessage(
            thread_id=thread1.id,
            message_id_header="<msg001@acme-supply.ru>",
            mailbox="procurement",
            from_address="sales@acme-supply.ru",
            to_addresses=["procurement@nashzavod.ru"],
            subject="Счёт №123 от АКМЕ за март",
            body_text="Добрый день! Направляем счёт №123 на оплату. С уважением, АКМЕ.",
            sent_at=now - timedelta(hours=4),
            received_at=now - timedelta(hours=4),
            has_attachments=True,
            attachment_count=1,
            attachments_meta=[{"filename": "schet_123.pdf", "size": 245760, "content_type": "application/pdf"}],
            is_inbound=True,
        )
        db.add(email1)
        db.flush()

        # ── Documents ────────────────────────────────────────────────────

        docs = [
            Document(
                file_name="schet_123_acme.pdf",
                file_hash="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
                file_size=245760,
                mime_type="application/pdf",
                storage_path="documents/a1/b2/a1b2c3d4e5f6...",
                page_count=2,
                doc_type=DocumentType.invoice,
                doc_type_confidence=0.95,
                status=DocumentStatus.needs_review,
                source_channel="email",
                source_email_id=email1.id,
            ),
            Document(
                file_name="kp_techno_podshipniki.pdf",
                file_hash="b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
                file_size=512000,
                mime_type="application/pdf",
                storage_path="documents/b2/c3/b2c3d4e5f6...",
                page_count=5,
                doc_type=DocumentType.commercial_offer,
                doc_type_confidence=0.88,
                status=DocumentStatus.needs_review,
                source_channel="email",
            ),
            Document(
                file_name="dogovor_acme_2025.pdf",
                file_hash="c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
                file_size=1024000,
                mime_type="application/pdf",
                storage_path="documents/c3/d4/c3d4e5f6a1...",
                page_count=12,
                doc_type=DocumentType.contract,
                doc_type_confidence=0.92,
                status=DocumentStatus.approved,
                source_channel="upload",
            ),
            Document(
                file_name="nakladnaya_sidorov_001.pdf",
                file_hash="d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5",
                file_size=180000,
                mime_type="application/pdf",
                storage_path="documents/d4/e5/d4e5f6a1b2...",
                page_count=1,
                doc_type=DocumentType.waybill,
                doc_type_confidence=0.85,
                status=DocumentStatus.ingested,
                source_channel="upload",
            ),
            Document(
                file_name="chertezh_flantsy.dwg",
                file_hash="e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6",
                file_size=2048000,
                mime_type="application/octet-stream",
                storage_path="documents/e5/f6/e5f6a1b2c3...",
                doc_type=DocumentType.drawing,
                status=DocumentStatus.approved,
                source_channel="upload",
            ),
        ]
        db.add_all(docs)
        db.flush()

        # ── Invoices ─────────────────────────────────────────────────────

        inv1 = Invoice(
            document_id=docs[0].id,
            invoice_number="123",
            invoice_date=now - timedelta(days=3),
            due_date=now + timedelta(days=27),
            currency="RUB",
            supplier_id=suppliers[0].id,
            buyer_id=buyer.id,
            subtotal=125_000.00,
            tax_amount=25_000.00,
            total_amount=150_000.00,
            status=InvoiceStatus.needs_review,
            overall_confidence=0.87,
        )
        db.add(inv1)
        db.flush()

        lines = [
            InvoiceLine(
                invoice_id=inv1.id,
                line_number=1,
                description="Болт М8×40 DIN 933 оцинк.",
                quantity=500,
                unit="шт",
                unit_price=15.00,
                amount=7_500.00,
                tax_rate=20.0,
                tax_amount=1_500.00,
                confidence=0.92,
            ),
            InvoiceLine(
                invoice_id=inv1.id,
                line_number=2,
                description="Гайка М8 DIN 934 оцинк.",
                quantity=500,
                unit="шт",
                unit_price=8.00,
                amount=4_000.00,
                tax_rate=20.0,
                tax_amount=800.00,
                confidence=0.94,
            ),
            InvoiceLine(
                invoice_id=inv1.id,
                line_number=3,
                description='Шайба М8 DIN 125 "А" оцинк.',
                quantity=1000,
                unit="шт",
                unit_price=3.50,
                amount=3_500.00,
                tax_rate=20.0,
                tax_amount=700.00,
                confidence=0.91,
            ),
            InvoiceLine(
                invoice_id=inv1.id,
                line_number=4,
                description="Шпилька М10×80 DIN 976",
                quantity=200,
                unit="шт",
                unit_price=55.00,
                amount=11_000.00,
                tax_rate=20.0,
                tax_amount=2_200.00,
                confidence=0.88,
            ),
            InvoiceLine(
                invoice_id=inv1.id,
                line_number=5,
                description="Подшипник SKF 6208-2RS",
                quantity=10,
                unit="шт",
                unit_price=9_900.00,
                amount=99_000.00,
                tax_rate=20.0,
                tax_amount=19_800.00,
                confidence=0.75,
            ),
        ]
        db.add_all(lines)

        # ── Pending Approval ─────────────────────────────────────────────

        approval = Approval(
            action_type=ApprovalActionType.invoice_approve,
            entity_type="invoice",
            entity_id=inv1.id,
            status=ApprovalStatus.pending,
            requested_by="sveta",
            assigned_to="procurement_user",
            context={
                "invoice_number": "123",
                "supplier": "АКМЕ Поставки",
                "total_amount": 150_000.00,
                "currency": "RUB",
                "line_count": 5,
            },
        )
        db.add(approval)

        # ── Canonical Items ───────────────────────────────────────────────
        canonical_data = [
            {"name": "Болт М8×30 ГОСТ 7798", "category": "Крепёж", "unit": "шт", "okpd2_code": "25.94.11", "gost": "ГОСТ 7798-70", "is_confirmed": True, "aliases": ["Болт М8", "Bolt M8x30"]},
            {"name": "Гайка М8 ГОСТ 5915", "category": "Крепёж", "unit": "шт", "okpd2_code": "25.94.12", "gost": "ГОСТ 5915-70", "is_confirmed": True, "aliases": ["Гайка М8"]},
            {"name": "Подшипник 6205 ГОСТ 8338", "category": "Подшипники", "unit": "шт", "okpd2_code": "28.15.20", "gost": "ГОСТ 8338-75", "is_confirmed": True, "aliases": ["6205", "Подшипник шариковый 6205"]},
            {"name": "Масло И-20А ГОСТ 20799", "category": "Смазочные материалы", "unit": "л", "okpd2_code": "19.20.29", "gost": "ГОСТ 20799-88", "is_confirmed": False, "aliases": ["И-20А", "Масло индустриальное"]},
            {"name": "Электрод МР-3 Ø3мм", "category": "Сварочные материалы", "unit": "кг", "okpd2_code": "25.99.29", "is_confirmed": True, "aliases": ["МР-3", "Электрод сварочный МР-3"]},
        ]
        canonical_items = []
        for cd in canonical_data:
            ci = CanonicalItem(**cd)
            db.add(ci)
            canonical_items.append(ci)
        db.flush()

        # ── Price History for canonical items ─────────────────────────────
        ph_entries = [
            PriceHistoryEntry(canonical_item_id=canonical_items[0].id, price=12.50, currency="RUB", recorded_at=datetime.now(timezone.utc) - timedelta(days=90), source="invoice"),
            PriceHistoryEntry(canonical_item_id=canonical_items[0].id, price=13.80, currency="RUB", recorded_at=datetime.now(timezone.utc) - timedelta(days=30), source="invoice"),
            PriceHistoryEntry(canonical_item_id=canonical_items[0].id, price=14.20, currency="RUB", recorded_at=datetime.now(timezone.utc) - timedelta(days=5), source="invoice"),
            PriceHistoryEntry(canonical_item_id=canonical_items[2].id, price=480.00, currency="RUB", recorded_at=datetime.now(timezone.utc) - timedelta(days=60), source="invoice"),
            PriceHistoryEntry(canonical_item_id=canonical_items[2].id, price=520.00, currency="RUB", recorded_at=datetime.now(timezone.utc) - timedelta(days=10), source="invoice"),
        ]
        for ph in ph_entries:
            db.add(ph)

        # ── Anomalies ─────────────────────────────────────────────────────
        anomalies = [
            AnomalyCard(
                anomaly_type=AnomalyType.price_spike,
                severity="warning",
                status=AnomalyStatus.open,
                entity_type="invoice",
                entity_id=str(inv1.id),
                title="Скачок цены: Болт М8×30 выше нормы на 38%",
                description="Цена позиции 14.20 руб превышает среднюю историческую (10.30 руб) на 37.9%.",
            ),
            AnomalyCard(
                anomaly_type=AnomalyType.new_supplier,
                severity="info",
                status=AnomalyStatus.open,
                entity_type="party",
                entity_id=str(suppliers[0].id),
                title="Новый поставщик: ООО «ТехноПром»",
                description="Первый счёт от данного поставщика. Рекомендуется проверить реквизиты.",
            ),
            AnomalyCard(
                anomaly_type=AnomalyType.duplicate,
                severity="critical",
                status=AnomalyStatus.open,
                entity_type="invoice",
                entity_id=str(inv1.id),
                title="Возможный дубликат: счёт №123 уже существует",
                description="Найден счёт с совпадающим номером, поставщиком и суммой.",
            ),
        ]
        for a in anomalies:
            db.add(a)

        # ── Calendar Events (Scenario 5: Proactive Follow-up) ─────────────
        now = datetime.now(timezone.utc)
        calendar_events = [
            CalendarEvent(
                title="Срок оплаты счёта №123 (ООО «ТехноПром»)",
                event_date=now + timedelta(days=3),
                event_type="due_date",
                entity_type="invoice",
                entity_id=inv1.id,
                source="extraction",
            ),
            CalendarEvent(
                title="Планируемая поставка: Болт М8×30 (50 шт.)",
                event_date=now + timedelta(days=7),
                event_type="delivery",
                entity_type="invoice",
                entity_id=inv1.id,
                source="extraction",
            ),
            CalendarEvent(
                title="Повторная проверка поставщика ООО «МашДеталь»",
                event_date=now + timedelta(days=14),
                event_type="meeting",
                entity_type="party",
                entity_id=suppliers[1].id,
                source="manual",
            ),
        ]
        for ev in calendar_events:
            db.add(ev)
        db.flush()

        # Reminder for the nearest deadline (Scenario 5 trigger)
        reminder = Reminder(
            calendar_event_id=calendar_events[0].id,
            entity_type="invoice",
            entity_id=inv1.id,
            remind_at=now + timedelta(days=2),
            message=f"Срок оплаты счёта приближается (через 3 дня). Требуется утверждение.",
            is_sent=False,
        )
        db.add(reminder)

        # ── Compare Session (Scenario 4: Compare КП) ──────────────────────
        compare_session = CompareSession(
            name="Сравнение КП: Болт М8×30 — заказ №2024-100",
            status="aligned",
            invoice_ids=[str(inv1.id)],
            alignment={
                "columns": ["Наименование", "Кол-во", "Цена", "Сумма", "Срок"],
                "rows": [
                    {
                        "item": "Болт М8×30",
                        "qty": 100,
                        "supplier_a": {"price": 14.20, "total": 1420.0, "lead_days": 7},
                        "supplier_b": {"price": 13.50, "total": 1350.0, "lead_days": 14},
                    }
                ],
                "recommendation": "supplier_b дешевле на 5%, но срок поставки +7 дней",
            },
        )
        db.add(compare_session)

        db.commit()
        print("Seed data created successfully!")
        print(f"  Suppliers: {len(suppliers)}")
        print(f"  Documents: {len(docs)}")
        print(f"  Invoices: 1 ({len(lines)} lines)")
        print(f"  Email threads: 2")
        print(f"  Pending approvals: 1")
        print(f"  Canonical items: {len(canonical_items)}")
        print(f"  Anomalies: {len(anomalies)}")
        print(f"  Calendar events: {len(calendar_events)}")
        print(f"  Compare sessions: 1")


if __name__ == "__main__":
    seed()
