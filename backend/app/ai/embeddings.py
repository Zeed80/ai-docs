"""Embedding service with registry-backed active model profile."""

from dataclasses import dataclass

import structlog

from app.ai.model_registry import ModelRegistry
from app.ai.router import AIRouter
from app.ai.schemas import AIRequest, AITask

logger = structlog.get_logger()

EMBED_MODEL = "qwen3-embedding:8b"
EMBED_DIM = 4096


@dataclass(frozen=True)
class EmbeddingProfile:
    model_key: str
    provider_model: str
    collection_name: str
    dimension: int
    distance_metric: str
    normalize: bool


def get_active_embedding_profile() -> EmbeddingProfile:
    # Ключ берётся из маршрутизации задач — того же места, которое правит GUI.
    # Раньше читался ai_config: второе хранилище тех же настроек, куда
    # значение попадает только при сохранении из интерфейса, и оно уже
    # расходилось с маршрутизацией.
    from app.ai.schemas import AITask
    from app.ai.task_routing import get_routing_for

    model_key = get_routing_for(AITask.EMBEDDING).primary or "local_embedding_ollama"
    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    model = registry.get_model(model_key)
    dimension = model.embedding_dimension or EMBED_DIM
    collection_name = embedding_collection_name(
        scope="documents",
        model_key=model.name,
        dimension=dimension,
        distance_metric=model.distance_metric,
    )
    if model.name == "local_embedding_ollama" and model.embedding_dimension == 768:
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


async def embed_text(
    text: str,
    profile: EmbeddingProfile | None = None,
    task_type: str = "passage",
) -> list[float]:
    """Generate an embedding vector using the active registry model."""
    active_profile = profile or get_active_embedding_profile()
    if not text or not text.strip():
        return [0.0] * active_profile.dimension

    prefixed_text = text
    if "qwen3" in active_profile.model_key.lower():
        prefix = "query: " if task_type == "query" else "passage: "
        prefixed_text = f"{prefix}{text}"

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter(registry)
    response = await router.run(
        AIRequest(
            task=AITask.EMBEDDING,
            input_text=prefixed_text[:32000],
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


async def embed_texts(
    texts: list[str],
    profile: EmbeddingProfile | None = None,
    task_type: str = "passage",
    batch_size: int = 32,
) -> list[list[float]]:
    """Embed many texts with as few model calls as possible.

    Ingesting a catalog used to call the embedding model once per position —
    thousands of round trips on the same GPU that parses the pages. Ollama's
    /api/embed takes an array, so a page's worth of positions costs one call.
    Providers that ignore ``input_texts`` fall back to per-text embedding, so
    the caller never has to care which one is configured.
    """
    active_profile = profile or get_active_embedding_profile()
    if not texts:
        return []

    prefix = ""
    if "qwen3" in active_profile.model_key.lower():
        prefix = "query: " if task_type == "query" else "passage: "

    registry = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml")
    router = AIRouter(registry)
    results: list[list[float]] = []

    for start in range(0, len(texts), max(1, batch_size)):
        chunk = texts[start : start + max(1, batch_size)]
        prepared = [f"{prefix}{(text or '').strip()}"[:32000] for text in chunk]
        vectors: list[list[float]] = []
        try:
            response = await router.run(
                AIRequest(
                    task=AITask.EMBEDDING,
                    input_text=prepared[0],
                    input_texts=prepared,
                    preferred_model=active_profile.model_key,
                    confidential=True,
                )
            )
            vectors = response.embeddings or []
        except Exception as exc:  # noqa: BLE001 — fall back to one-by-one
            logger.warning("embed_batch_failed", error=str(exc)[:200], size=len(chunk))

        if len(vectors) != len(chunk):
            vectors = [await embed_text(text, active_profile, task_type) for text in chunk]

        for index, vector in enumerate(vectors):
            if not vector:
                vectors[index] = [0.0] * active_profile.dimension
        results.extend(vectors)

    return results


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
