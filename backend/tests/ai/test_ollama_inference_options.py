from app.ai.providers.ollama import _inference_options
from app.ai.schemas import AIRequest, AITask


def test_ollama_inference_options_honor_bounded_per_task_context():
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"temperature": 0, "num_ctx": 8192}},
    )

    assert _inference_options(request) == {"temperature": 0, "num_ctx": 8192}


def test_ollama_inference_options_bound_context_to_service_limits():
    tiny = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"num_ctx": 100}},
    )
    huge = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"num_ctx": 100_000}},
    )

    assert _inference_options(tiny)["num_ctx"] == 4096
    assert _inference_options(huge)["num_ctx"] == 32768
