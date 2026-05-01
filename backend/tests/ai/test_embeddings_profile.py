from app.ai.embeddings import (
    EmbeddingProfile,
    embedding_collection_name,
    get_active_embedding_profile,
)


def test_embedding_collection_name_is_stable() -> None:
    assert (
        embedding_collection_name(
            scope="documents",
            model_key="local_embedding_vllm",
            dimension=1024,
            distance_metric="cosine",
        )
        == "documents__local_embedding_vllm__1024_cosine"
    )


def test_default_embedding_profile_uses_qwen3() -> None:
    profile = get_active_embedding_profile()

    assert profile.model_key == "qwen3_embedding_8b_ollama"
    assert profile.dimension == 4096
    assert profile.collection_name == "documents__qwen3_embedding_8b_ollama__4096_cosine"


def test_embedding_profile_serializes_for_api() -> None:
    profile = EmbeddingProfile(
        model_key="m",
        provider_model="provider-m",
        collection_name="documents__m__128_cosine",
        dimension=128,
        distance_metric="cosine",
        normalize=True,
    )

    assert profile.__dict__["collection_name"] == "documents__m__128_cosine"
