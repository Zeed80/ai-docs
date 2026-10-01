"""Persistent, shared budget ledgers for durable work orders."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import GUID, Base, TimestampMixin, UUIDPrimaryKey


class WorkBudgetLedger(UUIDPrimaryKey, TimestampMixin, Base):
    """One server-owned budget shared by a root WorkOrder and its descendants."""

    __tablename__ = "work_budget_ledgers"

    root_work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey(
            "work_orders.id",
            name="fk_work_budget_ledgers_root_work_order_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        unique=True,
        index=True,
    )
    owner_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    max_active_seconds: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    max_tool_attempts: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    max_llm_calls: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    max_replans: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    max_tokens: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    max_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    blocker: Mapped[dict | None] = mapped_column(JSON)


class WorkBudgetReservation(UUIDPrimaryKey, Base):
    """Immutable reservation identity plus an idempotent settlement."""

    __tablename__ = "work_budget_reservations"
    __table_args__ = (
        UniqueConstraint("ledger_id", "operation_key", name="uq_work_budget_operation"),
        Index("ix_work_budget_reservations_ledger_dimension", "ledger_id", "dimension"),
    )

    ledger_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("work_budget_ledgers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("work_orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    dimension: Mapped[str] = mapped_column(String(40), nullable=False)
    reserved_units: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="reserved", index=True)
    actual_units: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    actual_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    settlement_digest: Mapped[str | None] = mapped_column(String(64))
    blocker: Mapped[dict | None] = mapped_column(JSON)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
