from app.ai.providers.ollama import _inference_options, _recovered_text
from app.ai.schemas import AIRequest, AITask


def test_recovered_text_passes_through_a_normal_answer():
    request = AIRequest(task=AITask.CAD_SPEC_READ)
    assert (
        _recovered_text(
            '{"kind":"rotation"}',
            "some reasoning",
            model="m",
            request=request,
        )
        == '{"kind":"rotation"}'
    )


def test_recovered_text_falls_back_to_thinking_when_the_answer_field_is_empty():
    """Live-reproduced 2026-08-14: qwen3-vl:32b given a json_schema format put
    its entire valid answer into "thinking" and left the answer field empty,
    on every fragment question of a real CAD digitization — confirmed in
    production logs (reason=answer_went_to_thinking_field, task=cad_spec_read),
    with both Ollama thinking-suppression switches already set. The provider
    must recover it rather than report "the model said nothing"."""
    request = AIRequest(task=AITask.CAD_SPEC_READ)
    recovered = _recovered_text(
        "",
        '{"part":"Вал ступенчатый","kind":"rotation","bodies":1}',
        model="qwen3-vl:32b",
        request=request,
    )
    assert recovered == '{"part":"Вал ступенчатый","kind":"rotation","bodies":1}'


def test_recovered_text_handles_a_missing_answer_field_the_same_as_empty():
    request = AIRequest(task=AITask.CAD_SPEC_READ)
    assert _recovered_text(None, "the answer", model="m", request=request) == "the answer"


def test_recovered_text_stays_empty_when_both_fields_are_empty():
    """A genuinely silent model must still read as "nothing", not crash."""
    request = AIRequest(task=AITask.CAD_SPEC_READ)
    assert _recovered_text("", "", model="m", request=request) == ""
    assert _recovered_text(None, None, model="m", request=request) is None


def test_ollama_inference_options_honor_bounded_per_task_context():
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"temperature": 0, "num_ctx": 8192}},
    )

    assert _inference_options(request) == {
        "temperature": 0,
        "num_ctx": 8192,
        # Лимит вывода теперь ставится на КАЖДОМ пути, а не только в vision.
        "num_predict": 8192,
    }


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
    assert _inference_options(huge)["num_ctx"] == 65536


# ── Лимит вывода и сэмплирование ─────────────────────────────────────────────


def test_output_limit_is_applied_on_every_path_not_only_vision():
    """`num_predict` читался ТОЛЬКО в vision.

    В `chat`/`structured_extract` значение из metadata игнорировалось целиком,
    поэтому обрыв ответа на середине JSON ловился на одном пути и молча
    случался на двух других.
    """
    request = AIRequest(task=AITask.CAD_SPEC_READ, metadata={"num_predict": 6000})
    assert _inference_options(request)["num_predict"] == 6000


def test_a_missing_limit_falls_back_to_the_default():
    request = AIRequest(task=AITask.CAD_SPEC_READ)
    assert _inference_options(request)["num_predict"] == 8192


def test_an_absurd_limit_is_clamped():
    request = AIRequest(task=AITask.CAD_SPEC_READ, metadata={"num_predict": 10_000_000})
    assert _inference_options(request)["num_predict"] == 32768


def test_min_p_from_the_profile_actually_reaches_ollama():
    """Объявленный и молча отброшенный параметр хуже отсутствующего.

    `min_p` стоял в parameter_profiles и не доходил ни до одного провайдера: по
    настройкам видно одно, работает другое.
    """
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"min_p": 0.05}},
    )
    assert _inference_options(request)["min_p"] == 0.05


def test_the_context_ceiling_follows_the_models_own_window():
    """Константа на всех врала в обе стороны.

    Модель с окном 131072 получала 65536, а модель с окном 8192 могла получить
    запрос вчетверо больше, чем она держит. Окно знает каталог, ограничивает
    провайдер — значит каталог должен до него доехать.
    """
    big = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"num_ctx": 100_000}, "model_max_context_tokens": 131072},
    )
    assert _inference_options(big)["num_ctx"] == 65536  # общий потолок всё ещё держит

    small = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"num_ctx": 32768}, "model_max_context_tokens": 8192},
    )
    assert _inference_options(small)["num_ctx"] == 8192


def test_an_unknown_window_keeps_the_previous_ceiling():
    request = AIRequest(
        task=AITask.CAD_SPEC_READ,
        metadata={"inference_params": {"num_ctx": 100_000}},
    )
    assert _inference_options(request)["num_ctx"] == 65536
