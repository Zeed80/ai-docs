import pytest

from app.ai import task_routing as tr
from app.ai.embeddings import (
    EmbeddingProfile,
    embedding_collection_name,
    get_active_embedding_profile,
)
from app.ai.schemas import AITask


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


@pytest.fixture
def routing(monkeypatch):
    """Назначение задачи EMBEDDING — в памяти теста.

    Профиль собирается из ЖИВОЙ маршрутизации, а тест сравнивал его с
    конкретным именем модели. Стоило назначить в интерфейсе другой
    эмбеддер — и тест падал, сообщая об «ошибке» там, где просто изменилась
    настройка стенда.
    """
    store: dict[str, dict] = {}
    monkeypatch.setattr(tr, "_redis_get", lambda: dict(store) if store else None)
    monkeypatch.setattr(tr, "_redis_set", lambda value: (store.clear(), store.update(value)))

    def _assign(model_key: str) -> None:
        routing_for = tr.get_routing_for(AITask.EMBEDDING)
        tr.save_task_routing(
            AITask.EMBEDDING, routing_for.model_copy(update={"models": [model_key]})
        )

    return _assign


def test_profile_follows_the_assigned_model(routing) -> None:
    routing("qwen3_embedding_8b_ollama")
    profile = get_active_embedding_profile()

    assert profile.model_key == "qwen3_embedding_8b_ollama"
    assert profile.dimension == 4096
    assert profile.collection_name == "documents__qwen3_embedding_8b_ollama__4096_cosine"


def test_changing_the_assignment_changes_the_collection(routing) -> None:
    """Смена эмбеддера обязана менять имя коллекции.

    Иначе векторы двух разных моделей легли бы в одну коллекцию Qdrant — с
    разной размерностью и несравнимыми расстояниями.
    """
    routing("qwen3_embedding_8b_ollama")
    first = get_active_embedding_profile()

    routing("qwen3_embedding_4b_ollama")
    second = get_active_embedding_profile()

    assert second.model_key == "qwen3_embedding_4b_ollama"
    assert second.collection_name != first.collection_name
    assert str(second.dimension) in second.collection_name


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
