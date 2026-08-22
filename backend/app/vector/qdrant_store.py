"""Qdrant vector store — collection management, upsert, search."""

import structlog
import uuid
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    HnswConfigDiff,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    Query,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    VectorParams,
)

from app.config import settings

logger = structlog.get_logger()

COLLECTION = "documents"
COLLECTION_DRAWINGS = "drawings"
COLLECTION_DRAWING_FEATURES = "drawing_features"
COLLECTION_TOOL_CATALOG = "tool_catalog"
# Legacy default, kept only as a last-resort fallback. The real dimension comes
# from the ACTIVE embedding profile — hardcoding it meant that switching the
# embedding model left the fixed-name collections (drawings, drawing_features,
# tool_catalog) on the old size and every write failed on a dimension mismatch.
VECTOR_SIZE = 4096


def active_vector_size(default: int = VECTOR_SIZE) -> int:
    """Vector dimension of the currently assigned embedding model."""
    try:
        from app.ai.embeddings import get_active_embedding_profile

        return int(get_active_embedding_profile().dimension) or default
    except Exception:  # noqa: BLE001 — never block a write path on config lookup
        return default


def get_client() -> QdrantClient:
    kwargs: dict = {"url": settings.qdrant_url, "timeout": 10}
    if getattr(settings, "qdrant_api_key", ""):
        kwargs["api_key"] = settings.qdrant_api_key
    return QdrantClient(**kwargs)


def _collection_vector_size(client: QdrantClient, name: str) -> int | None:
    """Configured vector dimension of an existing collection (None if unknown)."""
    try:
        params = client.get_collection(name).config.params.vectors
    except Exception:  # noqa: BLE001
        return None
    size = getattr(params, "size", None)
    if size:
        return int(size)
    if isinstance(params, dict):
        for value in params.values():
            inner = getattr(value, "size", None)
            if inner:
                return int(inner)
    return None


def ensure_collection(
    collection_name: str = COLLECTION,
    vector_size: int | None = None,
    distance_metric: str = "cosine",
    *,
    recreate_on_mismatch: bool = False,
) -> None:
    """Create Qdrant collection if it doesn't exist.

    When it exists with a DIFFERENT vector size (the embedding model changed),
    either recreate it — losing its points, so the caller must reindex — or
    leave it alone and log loudly. Silently keeping the old size is the worst
    option: every subsequent upsert fails on a dimension mismatch.
    """
    vector_size = vector_size or active_vector_size()
    client = get_client()
    existing = {c.name for c in client.get_collections().collections}
    if collection_name in existing:
        current = _collection_vector_size(client, collection_name)
        if current is not None and current != vector_size:
            if not recreate_on_mismatch:
                logger.warning(
                    "qdrant_collection_dimension_mismatch",
                    collection=collection_name, existing=current, expected=vector_size,
                )
                return
            logger.warning(
                "qdrant_collection_recreated_for_new_dimension",
                collection=collection_name, existing=current, expected=vector_size,
            )
            client.delete_collection(collection_name)
            existing.discard(collection_name)
    if collection_name not in existing:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=vector_size,
                distance=_distance(distance_metric),
                hnsw_config=HnswConfigDiff(m=16, ef_construct=100),
            ),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    always_ram=True,
                )
            ),
        )
        for field, schema_type in [
            ("doc_type", PayloadSchemaType.KEYWORD),
            ("status", PayloadSchemaType.KEYWORD),
        ]:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=schema_type,
            )
        logger.info("qdrant_collection_created", collection=collection_name)
    else:
        logger.debug("qdrant_collection_exists", collection=collection_name)


def ensure_drawing_collections(
    vector_size: int | None = None, *, recreate_on_mismatch: bool = False
) -> None:
    """Create drawing-related Qdrant collections if they don't exist.

    ``vector_size`` defaults to the active embedding model's dimension.
    ``recreate_on_mismatch`` drops and rebuilds a collection whose stored
    dimension differs — data is lost, so only the explicit reindex path passes
    it; normal write paths log the mismatch instead of silently deleting.
    """
    vector_size = vector_size or active_vector_size()
    for collection_name, payload_indexes in [
        (
            COLLECTION_DRAWINGS,
            [("status", PayloadSchemaType.KEYWORD), ("drawing_number", PayloadSchemaType.KEYWORD)],
        ),
        (
            COLLECTION_DRAWING_FEATURES,
            [("drawing_id", PayloadSchemaType.KEYWORD), ("feature_type", PayloadSchemaType.KEYWORD)],
        ),
        (
            COLLECTION_TOOL_CATALOG,
            [("tool_type", PayloadSchemaType.KEYWORD), ("supplier_id", PayloadSchemaType.KEYWORD),
             ("is_active", PayloadSchemaType.KEYWORD),
             # Scoping a vector search to one catalog needs this indexed too.
             ("catalog_document_id", PayloadSchemaType.KEYWORD),
             ("has_image", PayloadSchemaType.KEYWORD)],
        ),
    ]:
        ensure_collection(
            collection_name=collection_name,
            vector_size=vector_size,
            recreate_on_mismatch=recreate_on_mismatch,
        )
        client = get_client()
        existing_collection = client.get_collection(collection_name)
        for field, schema_type in payload_indexes:
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field,
                    field_schema=schema_type,
                )
            except Exception:
                pass


def upsert_drawing(
    drawing_id: str,
    vector: list[float],
    *,
    drawing_number: str | None,
    status: str,
    filename: str,
    title: str | None = None,
    embedding_model: str | None = None,
) -> None:
    """Upsert drawing embedding into Qdrant."""
    client = get_client()
    client.upsert(
        collection_name=COLLECTION_DRAWINGS,
        points=[
            PointStruct(
                id=_stable_point_uuid(f"drawing:{drawing_id}"),
                vector=vector,
                payload={
                    "drawing_id": drawing_id,
                    "drawing_number": drawing_number or "",
                    "status": status,
                    "filename": filename,
                    "title": title or "",
                    "embedding_model": embedding_model or "",
                },
            )
        ],
    )


def upsert_drawing_feature(
    feature_id: str,
    vector: list[float],
    *,
    drawing_id: str,
    feature_type: str,
    name: str,
    description: str | None = None,
    embedding_model: str | None = None,
) -> None:
    """Upsert drawing feature embedding into Qdrant."""
    client = get_client()
    client.upsert(
        collection_name=COLLECTION_DRAWING_FEATURES,
        points=[
            PointStruct(
                id=_stable_point_uuid(f"drawing_feature:{feature_id}"),
                vector=vector,
                payload={
                    "feature_id": feature_id,
                    "drawing_id": drawing_id,
                    "feature_type": feature_type,
                    "name": name,
                    "description": description or "",
                    "embedding_model": embedding_model or "",
                },
            )
        ],
    )


def upsert_tool_catalog_entry(
    entry_id: str,
    vector: list[float],
    *,
    tool_type: str,
    name: str,
    supplier_id: str | None = None,
    diameter_mm: float | None = None,
    material: str | None = None,
    is_active: bool = True,
    embedding_model: str | None = None,
    part_number: str | None = None,
    catalog_document_id: str | None = None,
    catalog_page: int | None = None,
    has_image: bool = False,
    price_value: float | None = None,
) -> None:
    """Upsert tool catalog entry embedding into Qdrant.

    The payload carries the catalog and page so a vector hit can be filtered
    the same way the SQL branch is — a search scoped to one catalog must not
    quietly return positions from another one.
    """
    client = get_client()
    client.upsert(
        collection_name=COLLECTION_TOOL_CATALOG,
        points=[
            PointStruct(
                id=_stable_point_uuid(f"tool_catalog:{entry_id}"),
                vector=vector,
                payload={
                    "entry_id": entry_id,
                    "tool_type": tool_type,
                    "name": name,
                    "supplier_id": supplier_id or "",
                    "diameter_mm": diameter_mm,
                    "material": material or "",
                    "is_active": str(is_active).lower(),
                    "embedding_model": embedding_model or "",
                    "part_number": part_number or "",
                    "catalog_document_id": catalog_document_id or "",
                    "catalog_page": catalog_page,
                    "has_image": str(bool(has_image)).lower(),
                    "price_value": price_value,
                },
            )
        ],
    )


def search_drawing_features(
    query_vector: list[float],
    *,
    drawing_id: str | None = None,
    feature_type: str | None = None,
    limit: int = 20,
    score_threshold: float = 0.0,
) -> list[dict]:
    """Search drawing features by embedding similarity."""
    client = get_client()
    must = []
    if drawing_id:
        must.append(FieldCondition(key="drawing_id", match=MatchValue(value=drawing_id)))
    if feature_type:
        must.append(FieldCondition(key="feature_type", match=MatchValue(value=feature_type)))
    query_filter = Filter(must=must) if must else None
    response = client.query_points(
        collection_name=COLLECTION_DRAWING_FEATURES,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        score_threshold=score_threshold,
    )
    return [
        {
            "feature_id": hit.payload.get("feature_id", ""),
            "drawing_id": hit.payload.get("drawing_id", ""),
            "feature_type": hit.payload.get("feature_type", ""),
            "name": hit.payload.get("name", ""),
            "description": hit.payload.get("description", ""),
            "score": hit.score,
            "payload": hit.payload or {},
        }
        for hit in response.points
    ]


def search_tool_catalog(
    query_vector: list[float],
    *,
    tool_type: str | None = None,
    supplier_id: str | None = None,
    catalog_document_id: str | None = None,
    has_image: bool | None = None,
    limit: int = 20,
    score_threshold: float = 0.0,
) -> list[dict]:
    """Search tool catalog entries by embedding similarity."""
    client = get_client()
    must: list = [FieldCondition(key="is_active", match=MatchValue(value="true"))]
    if tool_type:
        must.append(FieldCondition(key="tool_type", match=MatchValue(value=tool_type)))
    if supplier_id:
        must.append(FieldCondition(key="supplier_id", match=MatchValue(value=supplier_id)))
    if catalog_document_id:
        must.append(
            FieldCondition(
                key="catalog_document_id", match=MatchValue(value=catalog_document_id)
            )
        )
    if has_image is not None:
        must.append(
            FieldCondition(key="has_image", match=MatchValue(value=str(has_image).lower()))
        )
    query_filter = Filter(must=must)
    response = client.query_points(
        collection_name=COLLECTION_TOOL_CATALOG,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        score_threshold=score_threshold,
    )
    return [
        {
            "entry_id": hit.payload.get("entry_id", ""),
            "tool_type": hit.payload.get("tool_type", ""),
            "name": hit.payload.get("name", ""),
            "supplier_id": hit.payload.get("supplier_id", ""),
            "diameter_mm": hit.payload.get("diameter_mm"),
            "material": hit.payload.get("material", ""),
            "score": hit.score,
            "payload": hit.payload or {},
        }
        for hit in response.points
    ]


def delete_drawing(drawing_id: str) -> None:
    """Delete all Qdrant points for a drawing."""
    from qdrant_client.models import FilterSelector
    client = get_client()
    for collection in [COLLECTION_DRAWINGS, COLLECTION_DRAWING_FEATURES]:
        id_field = "drawing_id" if collection == COLLECTION_DRAWING_FEATURES else None
        try:
            if collection == COLLECTION_DRAWINGS:
                client.delete(
                    collection_name=collection,
                    points_selector=FilterSelector(
                        filter=Filter(
                            must=[FieldCondition(key="drawing_id", match=MatchValue(value=drawing_id))]
                        )
                    ),
                )
            else:
                client.delete(
                    collection_name=collection,
                    points_selector=FilterSelector(
                        filter=Filter(
                            must=[FieldCondition(key="drawing_id", match=MatchValue(value=drawing_id))]
                        )
                    ),
                )
        except Exception:
            pass


def upsert_document(
    doc_id: str,
    vector: list[float],
    *,
    file_name: str,
    doc_type: str | None,
    status: str,
    source_channel: str | None = None,
    collection_name: str = COLLECTION,
    embedding_model: str | None = None,
) -> None:
    """Upsert document embedding into Qdrant."""
    client = get_client()
    client.upsert(
        collection_name=collection_name,
        points=[
            PointStruct(
                id=_uuid_to_uint64(doc_id),
                vector=vector,
                payload={
                    "doc_id": doc_id,
                    "file_name": file_name,
                    "doc_type": doc_type or "",
                    "status": status,
                    "source_channel": source_channel or "",
                    "embedding_model": embedding_model or "",
                },
            )
        ],
    )


def upsert_memory_embedding(
    *,
    point_id: str,
    vector: list[float],
    collection_name: str,
    payload: dict,
) -> None:
    client = get_client()
    client.upsert(
        collection_name=collection_name,
        points=[
            PointStruct(
                id=_stable_point_uuid(point_id),
                vector=vector,
                payload={**payload, "point_id": point_id},
            )
        ],
    )


def search_similar(
    query_vector: list[float],
    *,
    limit: int = 20,
    doc_type: str | None = None,
    status: str | None = None,
    content_types: list[str] | None = None,
    score_threshold: float = 0.0,
    collection_name: str = COLLECTION,
) -> list[dict]:
    """Search Qdrant for similar points. Returns list of {doc_id, score, payload}.

    ``content_types`` restricts the search to points of those payload
    content_type values (e.g. ["document_chunk", "evidence_span"]) — without it,
    document-level vectors crowd out chunk/evidence points in the shared
    collection, starving memory.search of fragment-level hits.
    """
    from qdrant_client.models import MatchAny

    client = get_client()

    must = []
    if doc_type:
        must.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if status:
        must.append(FieldCondition(key="status", match=MatchValue(value=status)))
    if content_types:
        must.append(FieldCondition(key="content_type", match=MatchAny(any=content_types)))

    query_filter = Filter(must=must) if must else None

    response = client.query_points(
        collection_name=collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        score_threshold=score_threshold,
    )

    return [
        {
            "doc_id": hit.payload.get("doc_id", ""),
            "score": hit.score,
            "file_name": hit.payload.get("file_name", ""),
            "doc_type": hit.payload.get("doc_type", ""),
            "status": hit.payload.get("status", ""),
            "payload": hit.payload or {},
        }
        for hit in response.points
    ]


def delete_tool_catalog_entry(entry_id: str) -> None:
    """Delete a single tool catalog entry from Qdrant by entry_id."""
    from qdrant_client.models import FilterSelector
    client = get_client()
    try:
        client.delete(
            collection_name=COLLECTION_TOOL_CATALOG,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="entry_id", match=MatchValue(value=entry_id))]
                )
            ),
        )
    except Exception as exc:
        logger.warning("qdrant_delete_tool_entry_failed", entry_id=entry_id, error=str(exc))


def delete_tool_catalog_by_supplier(supplier_id: str) -> None:
    """Delete all tool catalog entries for a given supplier from Qdrant."""
    from qdrant_client.models import FilterSelector
    client = get_client()
    try:
        client.delete(
            collection_name=COLLECTION_TOOL_CATALOG,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[FieldCondition(key="supplier_id", match=MatchValue(value=supplier_id))]
                )
            ),
        )
    except Exception as exc:
        logger.warning("qdrant_delete_supplier_catalog_failed", supplier_id=supplier_id, error=str(exc))


def delete_document(doc_id: str) -> None:
    from qdrant_client.models import PointIdsList
    client = get_client()
    client.delete(
        collection_name=COLLECTION,
        points_selector=PointIdsList(points=[_uuid_to_uint64(doc_id)]),
    )


def collection_count() -> int:
    return collection_count_for(COLLECTION)


def collection_count_for(collection_name: str = COLLECTION) -> int:
    client = get_client()
    try:
        info = client.get_collection(collection_name)
        return info.points_count or 0
    except Exception:
        return 0


def _uuid_to_uint64(uuid_str: str) -> int:
    """Convert UUID string to uint64 for Qdrant point ID."""
    import uuid as uuid_mod
    return uuid_mod.UUID(uuid_str).int & 0xFFFFFFFFFFFFFFFF


def _stable_point_uuid(point_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"document-invoices-ai:{point_id}"))


def _distance(distance_metric: str) -> Distance:
    normalized = distance_metric.lower()
    if normalized == "dot":
        return Distance.DOT
    if normalized == "euclid":
        return Distance.EUCLID
    return Distance.COSINE


def set_tool_catalog_payload(entry_id: str, payload: dict) -> None:
    """Update a catalog point's payload WITHOUT re-embedding it.

    Positions indexed before the catalog/page/image fields existed are invisible
    to the new filters ("в этом каталоге", "только с картинкой"). Re-embedding
    thousands of rows to add three keys would cost hours of GPU for nothing.
    """
    client = get_client()
    client.set_payload(
        collection_name=COLLECTION_TOOL_CATALOG,
        payload=payload,
        points=[_stable_point_uuid(f"tool_catalog:{entry_id}")],
    )
