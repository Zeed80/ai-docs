"""Tests for the graceful-degradation retry logic in the Celery task.

Focused on the branching this workstream added: a transient (node-unreachable)
error retries with backoff up to ``max_retries``, then gives up and marks the
record failed. Uses Celery's own eager-retry machinery (``.apply()``) rather
than hand-rolling a fake bound-task ``self`` — that keeps the test honest
about what Celery actually does with ``self.retry()``.
"""

from __future__ import annotations

import io

from app.ai.comfyui_client import ComfyUITransientError
from app.tasks import image_generation as img_gen_task


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (width, height), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_size(content: bytes) -> tuple[int, int]:
    from PIL import Image

    return Image.open(io.BytesIO(content)).size


def test_transient_error_retries_then_gives_up_and_marks_failed(monkeypatch):
    calls = {"n": 0}

    async def _raise_transient(generation_id, task_id):
        calls["n"] += 1
        raise ComfyUITransientError("ComfyUI node unreachable")

    monkeypatch.setattr(img_gen_task, "_run", _raise_transient)

    mark_failed_calls: list[str] = []

    async def _fake_mark_failed(gen_uuid, err, owner_sub=None):
        mark_failed_calls.append(err)

    monkeypatch.setattr(img_gen_task, "_mark_failed", _fake_mark_failed)

    result = img_gen_task.run_image_generation.apply(
        args=["00000000-0000-0000-0000-000000000001"]
    ).get(disable_sync_subtasks=False)

    # max_retries=3 on the task → 1 initial attempt + 3 retries = 4 calls,
    # then give up and mark failed exactly once (not on every retry).
    assert calls["n"] == 4
    assert len(mark_failed_calls) == 1
    assert "ComfyUI" in mark_failed_calls[0]
    assert "error" in result


def test_non_transient_error_is_not_retried(monkeypatch):
    """The wrapper only special-cases ComfyUITransientError — anything else
    (e.g. _run's own internal handling failing unexpectedly) fails the task
    immediately instead of being retried."""

    calls = {"n": 0}

    async def _raise_final(generation_id, task_id):
        calls["n"] += 1
        raise RuntimeError("not a ComfyUI transient issue")

    monkeypatch.setattr(img_gen_task, "_run", _raise_final)

    result = img_gen_task.run_image_generation.apply(
        args=["00000000-0000-0000-0000-000000000002"]
    )

    assert calls["n"] == 1  # no retry attempted
    assert result.failed()


def test_apply_eskd_style_appends_to_user_prompt():
    prompt, negative = img_gen_task._apply_eskd_style("вал 50h6", "размытие")
    assert prompt.startswith("вал 50h6")
    assert "ЕСКД" in prompt
    assert "без рамки листа" in prompt and "без углового штампа" in prompt
    assert negative.startswith("размытие")
    assert "угловой штамп" in negative


def test_apply_eskd_style_handles_empty_prompt_and_negative():
    prompt, negative = img_gen_task._apply_eskd_style(None, None)
    assert prompt.startswith("технический чертёж по ЕСКД")
    assert negative == img_gen_task._ESKD_NEGATIVE_SUFFIX


def test_apply_quality_preset_fast_matches_existing_workflow_defaults():
    """"fast" must reproduce exactly what every builtin workflow already
    ships with (steps=4, cfg=1, full Lightning LoRA) — it's a no-op for
    anyone not opting into "quality", not a behavior change."""
    values: dict = {}
    img_gen_task._apply_quality_preset(values, "fast")
    assert values == {"steps": 4, "cfg": 1.0, "lora_strength": 1.0}


def test_apply_quality_preset_quality_disables_lightning_lora():
    values: dict = {}
    img_gen_task._apply_quality_preset(values, "quality")
    assert values["lora_strength"] == 0.0
    assert values["steps"] > 4
    assert values["cfg"] > 1.0


def test_apply_quality_preset_none_or_unknown_leaves_values_untouched():
    values = {"prompt": "x"}
    img_gen_task._apply_quality_preset(values, None)
    assert values == {"prompt": "x"}
    img_gen_task._apply_quality_preset(values, "ultra-mega-mode")
    assert values == {"prompt": "x"}


def test_apply_quality_preset_never_overrides_an_explicit_value():
    """A user-chosen numeric steps/cfg/lora_strength always wins over the
    named preset — the preset only fills in what's still unset."""
    values = {"steps": 10, "cfg": 2.0}
    img_gen_task._apply_quality_preset(values, "fast")
    assert values["steps"] == 10  # not overwritten to the preset's 4
    assert values["cfg"] == 2.0  # not overwritten to the preset's 1.0
    assert values["lora_strength"] == 1.0  # unset -> filled from preset


def test_reconcile_result_size_skips_aspect_mismatch_to_avoid_vertical_compression():
    source = _png(800, 1200)
    result = _png(1024, 1024)

    out, changed, reason = img_gen_task._reconcile_result_size(result, source)

    assert changed is False
    assert reason == "aspect-mismatch"
    assert _png_size(out) == (1024, 1024)


def test_reconcile_result_size_resizes_only_when_aspect_matches():
    source = _png(800, 1200)
    result = _png(1210, 1800)

    out, changed, reason = img_gen_task._reconcile_result_size(result, source)

    assert changed is True
    assert reason == "resized"
    assert _png_size(out) == (1200, 1800)
