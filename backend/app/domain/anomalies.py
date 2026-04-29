"""Pydantic schemas for Anomaly Detection domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class AnomalyCardOut(BaseModel):
    id: uuid.UUID
    anomaly_type: str
    severity: str
    status: str
    entity_type: str
    entity_id: uuid.UUID
    title: str
    description: str | None = None
    details: dict | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    resolution_comment: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AnomalyCheckRequest(BaseModel):
    invoice_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None


class AnomalyCheckResponse(BaseModel):
    entity_id: uuid.UUID
    anomalies_found: int
    anomalies: list[AnomalyCardOut]


class AnomalyCreateRequest(BaseModel):
    anomaly_type: str
    severity: str = "warning"
    entity_type: str
    entity_id: uuid.UUID
    title: str
    description: str | None = None
    details: dict | None = None


class AnomalyResolveRequest(BaseModel):
    resolution: str  # resolved, false_positive
    comment: str | None = None


class AnomalyExplainResponse(BaseModel):
    anomaly_id: uuid.UUID
    anomaly_type: str
    title: str
    explanation: str
    suggested_actions: list[str]
    context: dict
