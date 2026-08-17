from pathlib import Path

from app.ai import model_registry as mr
from app.ai.model_registry import ModelRegistry
from app.ai.schemas import AITask, ModelStatus, ProviderKind


REGISTRY_PATH = Path(__file__).parent.parent.parent / "app" / "ai" / "config" / "model_registry.yaml"


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
    # Reranking is LLM-as-reranker (generate+logprobs): dedicated reranker GGUFs
    # are broken on Ollama 0.30.x, so the route falls back to small instruct models.
    assert registry.get_route(AITask.RERANKING).fallback_chain == [
        "gemma4_e2b_ollama",
        "qwen3_5_9b_ollama",
    ]


def test_model_promotion_is_in_memory_and_explicit() -> None:
    registry = ModelRegistry.from_yaml(REGISTRY_PATH)

    registry.promote_model("local_embedding_vllm", ModelStatus.STAGING)

    assert registry.get_model("local_embedding_vllm").status == ModelStatus.STAGING


def test_thinking_levels_override_applies_to_yaml_defined_model(monkeypatch) -> None:
    """The live differential probe (or manual curation) must be able to
    attach a level determination to a model_registry.yaml-defined entry —
    unlike the catalog overlay (_load_catalog_overlay, which uses setdefault
    and can never touch an already-YAML-defined key), the thinking-override
    path is read AFTER the YAML+overlay merge and applies to any key.
    """
    monkeypatch.setattr(
        mr,
        "_load_thinking_overrides",
        lambda: {"qwen3_6_35b_apex_ollama": {"enabled": None, "level": None, "levels": []}},
    )
    registry = ModelRegistry.from_yaml(REGISTRY_PATH)
    cap = registry.get_model("qwen3_6_35b_apex_ollama")

    # A negative verdict (probed, unsupported) — real determination, not
    # "nothing happened". thinking_enabled must be untouched (override
    # provided enabled=None, so the YAML value survives).
    assert cap.thinking_levels == []
    assert cap.thinking_levels_probed is True
    assert cap.thinking_enabled is False  # unchanged from YAML


def test_thinking_levels_override_positive_verdict(monkeypatch) -> None:
    monkeypatch.setattr(
        mr,
        "_load_thinking_overrides",
        lambda: {
            "qwen3_6_35b_apex_ollama": {
                "enabled": None, "level": "high", "levels": ["low", "medium", "high"],
            }
        },
    )
    registry = ModelRegistry.from_yaml(REGISTRY_PATH)
    cap = registry.get_model("qwen3_6_35b_apex_ollama")

    assert cap.thinking_levels == ["low", "medium", "high"]
    assert cap.thinking_levels_probed is True
    assert cap.thinking_level_default == "high"
