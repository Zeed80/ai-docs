"""Client for the multimodal embedding sidecar (infra/vl-embedding).

Why this exists next to ``app.ai.embeddings``: that module embeds TEXT through
Ollama, and Ollama's embedding API accepts an ``images`` field and silently
ignores it — the vector describes the text alone, so image search built on it
would have quietly returned text matches. The sidecar runs Qwen3-VL-Embedding,
which puts pictures and text into one vector space.

That shared space is the property everything here relies on: a photo of a tool,
the crop of that tool printed in a supplier catalog, and the words «алмазный
круг 100x10» are all comparable directly, so ONE collection answers
search-by-photo, search-by-words and "more like this".

Degraded mode: every call returns None instead of raising when the sidecar is
down or has no GPU room. Visual search is an enrichment of catalog search — the
text/vector path must keep working without it, exactly like the rest of the
stack works without the agent.
"""

from __future__ import annotations

import base64

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger()

# The model is instruction-aware; the model card measures +1..5 % from a task
# instruction, and recommends writing it in English whatever the content is.
QUERY_PROMPT = "Find the catalog product that matches this query."
DOCUMENT_PROMPT = "Represent this catalog product for retrieval."

# The sidecar refuses larger batches (one GPU, and a big batch multiplies peak
# VRAM rather than throughput).
MAX_BATCH = 16


class VLEmbeddingUnavailable(RuntimeError):
    """The sidecar could not answer — caller decides whether that is fatal."""


async def _post(path: str, payload: dict | None = None) -> dict | None:
    url = f"{settings.vl_embedding_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=settings.vl_embedding_timeout_seconds) as client:
            response = (
                await client.get(url) if payload is None else await client.post(url, json=payload)
            )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 — degraded mode is the contract
        logger.warning("vl_embedding_unavailable", path=path, error=str(exc))
        return None


async def vl_info() -> dict | None:
    """Model name, vector dimension and device — None when unavailable.

    The dimension is read from the running model rather than hard-coded: it is
    part of the Qdrant collection's schema, and guessing it wrong creates a
    collection that every later upsert fails against.
    """
    return await _post("/info")


async def embed_multimodal(
    items: list[dict],
    *,
    prompt: str | None = None,
) -> list[list[float]] | None:
    """Embed a batch of {"text": str, "image": bytes} items.

    Returns one vector per item in the same order, or None if the sidecar is
    unavailable. Batches larger than MAX_BATCH are split.
    """
    if not items:
        return []

    vectors: list[list[float]] = []
    for start in range(0, len(items), MAX_BATCH):
        chunk = items[start : start + MAX_BATCH]
        payload_items = []
        for item in chunk:
            entry: dict[str, str] = {}
            text = (item.get("text") or "").strip()
            if text:
                entry["text"] = text[:2000]
            image = item.get("image")
            if image:
                entry["image_base64"] = base64.b64encode(image).decode("ascii")
            if not entry:
                # An item with neither text nor picture would shift every
                # following vector by one if it were dropped silently.
                logger.warning("vl_embedding_empty_item", index=start)
                return None
            payload_items.append(entry)

        body: dict = {"items": payload_items}
        if prompt:
            body["prompt"] = prompt
        result = await _post("/embed", body)
        if not result or "embeddings" not in result:
            return None
        vectors.extend(result["embeddings"])

    if len(vectors) != len(items):
        logger.warning(
            "vl_embedding_count_mismatch", requested=len(items), returned=len(vectors)
        )
        return None
    return vectors


async def embed_query(text: str | None = None, image: bytes | None = None) -> list[float] | None:
    """Embed one search query — words, a photo, or both."""
    if not text and not image:
        return None
    vectors = await embed_multimodal(
        [{"text": text, "image": image}], prompt=QUERY_PROMPT
    )
    return vectors[0] if vectors else None
