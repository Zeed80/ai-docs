"""Pydantic schemas for Drawings, DrawingFeatures, Contours, Dimensions, Surfaces, GDT, Tool Bindings."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from app.db.models import (
    DrawingFeatureType,
    DrawingStatus,
    FeatureDimType,
    FeaturePrimitiveType,
    RoughnessType,
    ToolSourceEnum,
)


# ── Contours ──────────────────────────────────────────────────────────────────


class FeatureContourCreate(BaseModel):
    primitive_type: FeaturePrimitiveType
    params: dict[str, Any]
    layer: str | None = None
    line_type: str = "solid"
    color: str | None = None
    sort_order: int = 0


class FeatureContourUpdate(BaseModel):
    primitive_type: FeaturePrimitiveType | None = None
    params: dict[str, Any] | None = None
    layer: str | None = None
    line_type: str | None = None
    color: str | None = None
    sort_order: int | None = None
    is_user_edited: bool | None = None


class FeatureContourOut(BaseModel):
    id: uuid.UUID
    feature_id: uuid.UUID
    primitive_type: FeaturePrimitiveType
    params: dict[str, Any]
    layer: str | None = None
    line_type: str
    color: str | None = None
    sort_order: int
    is_user_edited: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Dimensions ────────────────────────────────────────────────────────────────


class FeatureDimensionCreate(BaseModel):
    dim_type: FeatureDimType
    nominal: float
    upper_tol: float | None = None
    lower_tol: float | None = None
    unit: str = "mm"
    fit_system: str | None = None
    label: str | None = None
    annotation_position: dict | None = None
    is_reference: bool = False


class FeatureDimensionUpdate(BaseModel):
    dim_type: FeatureDimType | None = None
    nominal: float | None = None
    upper_tol: float | None = None
    lower_tol: float | None = None
    unit: str | None = None
    fit_system: str | None = None
    label: str | None = None
    annotation_position: dict | None = None
    is_reference: bool | None = None


class FeatureDimensionOut(BaseModel):
    id: uuid.UUID
    feature_id: uuid.UUID
    dim_type: FeatureDimType
    nominal: float
    upper_tol: float | None = None
    lower_tol: float | None = None
    unit: str
    fit_system: str | None = None
    label: str | None = None
    annotation_position: dict | None = None
    is_reference: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Surfaces ──────────────────────────────────────────────────────────────────


class FeatureSurfaceCreate(BaseModel):
    roughness_type: RoughnessType = RoughnessType.Ra
    value: float
    direction: str | None = None
    lay_symbol: str | None = None
    machining_required: bool = True
    annotation_position: dict | None = None


class FeatureSurfaceUpdate(BaseModel):
    roughness_type: RoughnessType | None = None
    value: float | None = None
    direction: str | None = None
    lay_symbol: str | None = None
    machining_required: bool | None = None
    annotation_position: dict | None = None


class FeatureSurfaceOut(BaseModel):
    id: uuid.UUID
    feature_id: uuid.UUID
    roughness_type: RoughnessType
    value: float
    direction: str | None = None
    lay_symbol: str | None = None
    machining_required: bool
    annotation_position: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── GDT ───────────────────────────────────────────────────────────────────────


class FeatureGDTCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=50)
    tolerance_value: float
    tolerance_zone: str | None = None
    datum_reference: str | None = None
    material_condition: str | None = None
    annotation_position: dict | None = None


class FeatureGDTUpdate(BaseModel):
    symbol: str | None = None
    tolerance_value: float | None = None
    tolerance_zone: str | None = None
    datum_reference: str | None = None
    material_condition: str | None = None
    annotation_position: dict | None = None


class FeatureGDTOut(BaseModel):
    id: uuid.UUID
    feature_id: uuid.UUID
    symbol: str
    tolerance_value: float
    tolerance_zone: str | None = None
    datum_reference: str | None = None
    material_condition: str | None = None
    annotation_position: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Tool Binding ──────────────────────────────────────────────────────────────


class FeatureToolBindingCreate(BaseModel):
    tool_source: ToolSourceEnum
    warehouse_item_id: uuid.UUID | None = None
    catalog_entry_id: uuid.UUID | None = None
    manual_description: str | None = None
    cutting_parameters: dict | None = None
    notes: str | None = None
    bound_by: str = "user"


class FeatureToolBindingUpdate(BaseModel):
    tool_source: ToolSourceEnum | None = None
    warehouse_item_id: uuid.UUID | None = None
    catalog_entry_id: uuid.UUID | None = None
    manual_description: str | None = None
    cutting_parameters: dict | None = None
    notes: str | None = None


class FeatureToolBindingOut(BaseModel):
    id: uuid.UUID
    feature_id: uuid.UUID
    tool_source: ToolSourceEnum
    warehouse_item_id: uuid.UUID | None = None
    catalog_entry_id: uuid.UUID | None = None
    manual_description: str | None = None
    cutting_parameters: dict | None = None
    notes: str | None = None
    bound_by: str
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# ── Drawing Features ──────────────────────────────────────────────────────────


class DrawingFeatureCreate(BaseModel):
    feature_type: DrawingFeatureType
    name: str = Field(..., min_length=1, max_length=300)
    description: str | None = None
    sort_order: int = 0
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    contours: list[FeatureContourCreate] = []
    dimensions: list[FeatureDimensionCreate] = []
    surfaces: list[FeatureSurfaceCreate] = []
    gdt_annotations: list[FeatureGDTCreate] = []
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class DrawingFeatureUpdate(BaseModel):
    feature_type: DrawingFeatureType | None = None
    name: str | None = None
    description: str | None = None
    sort_order: int | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class DrawingFeatureOut(BaseModel):
    id: uuid.UUID
    drawing_id: uuid.UUID
    feature_type: DrawingFeatureType
    name: str
    description: str | None = None
    sort_order: int
    confidence: float
    reviewed_at: datetime | None = None
    reviewed_by: str | None = None
    contours: list[FeatureContourOut] = []
    dimensions: list[FeatureDimensionOut] = []
    surfaces: list[FeatureSurfaceOut] = []
    gdt_annotations: list[FeatureGDTOut] = []
    tool_binding: FeatureToolBindingOut | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


# ── Drawing ───────────────────────────────────────────────────────────────────


class DrawingCreate(BaseModel):
    document_id: uuid.UUID | None = None
    drawing_number: str | None = None
    revision: str | None = None
    filename: str
    format: str
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class DrawingUpdate(BaseModel):
    drawing_number: str | None = None
    revision: str | None = None
    title_block: dict | None = None
    status: DrawingStatus | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )


class DrawingOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID | None = None
    drawing_number: str | None = None
    revision: str | None = None
    filename: str
    format: str
    svg_path: str | None = None
    thumbnail_path: str | None = None
    title_block: dict | None = None
    bounding_box: dict | None = None
    status: DrawingStatus
    analysis_error: str | None = None
    celery_task_id: str | None = None
    metadata_: dict | None = Field(
        None,
        validation_alias=AliasChoices("metadata_", "metadata"),
        serialization_alias="metadata",
    )
    created_at: datetime
    updated_at: datetime | None = None

    model_config = {"from_attributes": True, "populate_by_name": True}


class DrawingWithFeaturesOut(DrawingOut):
    features: list[DrawingFeatureOut] = []


class DrawingListResponse(BaseModel):
    items: list[DrawingOut]
    total: int
    page: int
    page_size: int


class DrawingUploadResponse(BaseModel):
    drawing_id: uuid.UUID
    task_id: str | None = None
    message: str


class DrawingAnalysisRequest(BaseModel):
    model: str | None = None
    force: bool = False


class ContoursUpdateRequest(BaseModel):
    contours: list[FeatureContourCreate]


class DrawingFeatureReviewRequest(BaseModel):
    reviewed_by: str = "user"


class DrawingDeleteResult(BaseModel):
    drawing_id: uuid.UUID
    deleted: int = 0


class DrawingBulkDeleteRequest(BaseModel):
    drawing_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    delete_files: bool = True


class DrawingBulkDeleteResponse(BaseModel):
    deleted: int = 0
    missing: int = 0
    results: list[DrawingDeleteResult] = []
