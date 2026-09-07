"""Несколько кадров получает только та модель, которая их принимает.

Плотный лист режется на тайлы источникового разрешения — иначе мелкие выноски
не читаются. Но принимать несколько изображений за один запрос умеет не всякая
модель, и каталог это знает: ``supports_multi_image`` по умолчанию False, и у
большинства записей он именно такой.

Читатель чертежа слал все тайлы не глядя. Модель, берущая одно изображение,
видела первый кадр и молча теряла остальные — а спрошена была про весь лист.
Тот же учёт возможности давно делает ``drawing_extractor``; здесь его не было.
"""

from __future__ import annotations

import io

from PIL import Image

from app.ai.cad_recognize.spec_vectorize import _model_takes_many_images, _spec_images


def _sheet(width: int, height: int) -> Image.Image:
    return Image.new("RGB", (width, height), "white")


def test_a_small_sheet_is_one_frame_and_needs_no_tiling():
    """1129×797 — размер живого z4-r4.jpg: резать нечего."""
    images, descriptions, coverage = _spec_images(_sheet(1129, 797))

    assert len(images) == 1
    assert coverage == 1.0
    assert "overview" in descriptions[0]


def test_a_large_sheet_is_tiled_at_source_resolution():
    images, _descriptions, coverage = _spec_images(_sheet(3000, 2200))

    assert len(images) > 1
    assert coverage > 0.9


def test_only_a_declared_multi_image_model_is_offered_many_frames():
    # qwen3-vl объявлен в каталоге как принимающий несколько изображений.
    assert _model_takes_many_images("qwen3_vl_32b_ollama") is True
    # glm-ocr — явно нет.
    assert _model_takes_many_images("glm_ocr_ollama") is False


def test_an_unknown_model_is_treated_as_single_image():
    """Ошибиться в сторону меньшего дешевле.

    Прислать девять кадров модели, которая берёт один, — потерять восемь молча
    и при этом спросить про весь лист.
    """
    assert _model_takes_many_images("модели-такой-нет") is False
    assert _model_takes_many_images(None) is False


def _read_with(model_key: str, monkeypatch) -> tuple[int, dict]:
    """Прогнать read_drawing_spec на большом листе и вернуть (кадров, спек)."""
    import asyncio
    import json

    from app.ai.cad_recognize import spec_vectorize
    from app.ai.schemas import AIResponse, ProviderKind

    sent: dict = {}

    class _Router:
        async def run(self, request):
            sent["images"] = len(request.images)
            return AIResponse(
                task=request.task,
                provider=ProviderKind.OLLAMA,
                model=model_key,
                text=json.dumps(
                    {
                        "schema_version": 1,
                        "part": "Вал",
                        "main_view": {
                            "type": "тело вращения (вал)",
                            "outer": [
                                {"diameter_mm": 30, "length_mm": 40},
                                {"diameter_mm": 50, "length_mm": 60},
                            ],
                        },
                    }
                ),
            )

    monkeypatch.setattr(spec_vectorize, "_first_vision_model", lambda _task: (model_key, True))
    monkeypatch.setattr(
        "app.ai.task_routing.get_routing_for",
        lambda _t: __import__("app.ai.task_routing", fromlist=["TaskRouting"]).TaskRouting(
            task="cad_spec_read", models=[model_key]
        ),
    )

    buffer = io.BytesIO()
    _sheet(3000, 2200).save(buffer, format="PNG")
    spec = asyncio.run(spec_vectorize.read_drawing_spec(buffer.getvalue(), router=_Router()))
    return sent.get("images", 0), spec


def test_a_single_image_model_gets_one_frame_and_the_cost_is_recorded(monkeypatch):
    """Показали меньше, чем нарезали, — это должно быть видно в спеке."""
    frames, spec = _read_with("glm_ocr_ollama", monkeypatch)

    assert frames == 1, "модели, берущей одно изображение, нельзя слать тайлы"
    notes = " ".join(spec.get("optional_unresolved") or [])
    assert "принимает одно изображение" in notes
    assert "мелкие выноски" in notes


def test_a_multi_image_model_still_gets_every_tile(monkeypatch):
    frames, spec = _read_with("qwen3_vl_32b_ollama", monkeypatch)

    assert frames > 1
    notes = " ".join(spec.get("optional_unresolved") or [])
    assert "принимает одно изображение" not in notes
