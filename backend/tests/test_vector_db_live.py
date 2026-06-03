"""Live vector-DB test: embedding model → Qdrant upsert → semantic retrieval.

Verifies that invoice text is embedded by the configured model and that semantic
search returns the right invoice (text data is sufficient for invoices). Uses a
TEMPORARY Qdrant collection and touches no production data.

Requires Qdrant + the embedding model (Ollama). Skips cleanly when unreachable
(e.g. on a host that can't reach the internal Qdrant), so it is safe in the
committed suite. Inside the backend container / CI with services it runs:

    PYTHONPATH=/app python3 -m pytest tests/test_vector_db_live.py -s
"""

from __future__ import annotations

import asyncio
import uuid

import pytest


def _services_up() -> bool:
    try:
        from app.vector.qdrant_store import get_client

        get_client().get_collections()
        return True
    except Exception:
        return False


_DOCS = {
    "фреза.pdf": 'Счёт ООО "НВС Компани" ИНН 7707083893. Фреза концевая твердосплавная '
                 "Ø10, сверло спиральное Ø5. Металлорежущий инструмент для станка.",
    "графит.pdf": 'Счёт ООО "Графит-Гарант". Графитовый блок ЭГ-15, графитовые электроды. '
                  "Углеродные материалы для электроэрозионной обработки.",
    "подшипник.pdf": 'Счёт АО "Снабжение". Подшипник шариковый 6205, подшипник роликовый. '
                     "Опорные узлы для валов и шпинделей.",
}


@pytest.mark.live
def test_invoice_vector_indexing_and_semantic_search() -> None:
    if not _services_up():
        pytest.skip("Qdrant/embedding services not reachable")

    from app.ai.embeddings import embed_text, get_active_embedding_profile
    from app.vector.qdrant_store import ensure_collection, get_client, upsert_document

    async def run() -> None:
        profile = get_active_embedding_profile()
        temp = f"vector_test_{uuid.uuid4().hex[:8]}"
        ensure_collection(collection_name=temp, vector_size=profile.dimension)
        client = get_client()
        try:
            for file_name, text in _DOCS.items():
                vec = await embed_text(text)
                assert len(vec) == profile.dimension and any(vec)
                upsert_document(
                    str(uuid.uuid4()), vec, file_name=file_name, doc_type="invoice",
                    status="approved", collection_name=temp,
                )

            def _search(qvec, limit):
                try:
                    return client.query_points(collection_name=temp, query=qvec, limit=limit).points
                except Exception:
                    return client.search(collection_name=temp, query_vector=qvec, limit=limit)

            # Semantic match without shared exact words → cutting-tool invoice.
            hits = _search(await embed_text("режущий инструмент для обработки металла на станке с ЧПУ"), 3)
            assert hits[0].payload.get("file_name") == "фреза.pdf", [
                (h.payload.get("file_name"), round(h.score, 3)) for h in hits
            ]
            # Distinct query → graphite invoice.
            hits2 = _search(await embed_text("углеродные электроды для электроэрозии"), 1)
            assert hits2[0].payload.get("file_name") == "графит.pdf"
        finally:
            client.delete_collection(temp)

    asyncio.run(run())
