from pathlib import Path

from backend.app.ai.model_registry import ModelRegistry
from backend.app.ai.schemas import AITask, ModelStatus, ProviderKind


REGISTRY_PATH = Path("backend/app/ai/config/model_registry.yaml")


def test_registry_loads_baseline_models() -> None:
    registry = ModelRegistry.from_yaml(REGISTRY_PATH)

    assert ProviderKind.OLLAMA in registry.providers
    assert "gemma4_e4b_ollama" in registry.models
    assert registry.get_route(AITask.INVOICE_OCR).local_only is True
    embedding = registry.get_model("local_embedding_ollama")
    assert embedding.embedding_dimension == 768
    assert embedding.distance_metric == "cosine"
    assert embedding.supports_batching is True
    reranker = registry.get_model("local_reranker_openai_compatible")
    assert "rerank" in {modality.value for modality in reranker.modalities}
    assert registry.get_route(AITask.RERANKING).fallback_chain == [
        "local_reranker_openai_compatible"
    ]


def test_model_promotion_is_in_memory_and_explicit() -> None:
    registry = ModelRegistry.from_yaml(REGISTRY_PATH)

    registry.promote_model("local_embedding_vllm", ModelStatus.STAGING)

    assert registry.get_model("local_embedding_vllm").status == ModelStatus.STAGING
