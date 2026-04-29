"""Embedding service with registry-backed active model profile."""

from dataclasses import dataclass

import structlog

from app.ai.model_registry import ModelRegistry
from app.ai.router import AIRouter
from app.ai.schemas import AIRequest, AITask

logger = structlog.get_logger()

EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768


@dataclass(frozen=True)
class EmbeddingProfile:
    model_key: str
    provider_model: str
    collection_name: str
    dimension: int
    distance_metric: str
    normalize: bool


def get_active_embedding_profile() -> EmbeddingProfile:
    from app.api.ai_settings import get_ai_config

    config = get_ai_config()
    model_key = config.get("embedding_model") or "local_embedding_ollama"
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    model = registry.get_model(model_key)
    dimension = model.embedding_dimension or EMBED_DIM
    collection_name = embedding_collection_name(
        scope="documents",
        model_key=model.name,
        dimension=dimension,
        distance_metric=model.distance_metric,
    )
    if model.name == "local_embedding_ollama" and dimension == EMBED_DIM:
        collection_name = "documents"
    return EmbeddingProfile(
        model_key=model.name,
        provider_model=model.provider_model,
        collection_name=collection_name,
        dimension=dimension,
        distance_metric=model.distance_metric,
        normalize=model.normalize_embeddings,
    )


def embedding_collection_name(
    *,
    scope: str,
    model_key: str,
    dimension: int,
    distance_metric: str,
) -> str:
    safe_model = "".join(ch if ch.isalnum() else "_" for ch in model_key.lower()).strip("_")
    return f"{scope}__{safe_model}__{dimension}_{distance_metric}"


async def embed_text(text: str, profile: EmbeddingProfile | None = None) -> list[float]:
    """Generate an embedding vector using the active registry model."""
    active_profile = profile or get_active_embedding_profile()
    if not text or not text.strip():
        return [0.0] * active_profile.dimension

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter(registry)
    response = await router.run(
        AIRequest(
            task=AITask.EMBEDDING,
            input_text=text[:8000],
            preferred_model=active_profile.model_key,
            confidential=True,
        )
    )
    vec = response.embedding or []

    if not vec:
        logger.warning("embed_empty_result", text_len=len(text))
        return [0.0] * active_profile.dimension

    if len(vec) != active_profile.dimension:
        logger.warning(
            "embed_dimension_mismatch",
            expected=active_profile.dimension,
            actual=len(vec),
            model=active_profile.model_key,
        )
    logger.debug("embedded", dim=len(vec), text_len=len(text), model=active_profile.model_key)
    return vec


def build_document_text(
    file_name: str,
    doc_type: str | None,
    extraction_fields: list[dict] | None = None,
) -> str:
    """Build text representation of a document for embedding."""
    parts = [file_name]

    if doc_type:
        parts.append(f"тип: {doc_type}")

    if extraction_fields:
        for field in extraction_fields:
            name = field.get("field_name", "")
            value = field.get("corrected_value") or field.get("field_value") or ""
            if value:
                parts.append(f"{name}: {value}")

    return " | ".join(parts)
