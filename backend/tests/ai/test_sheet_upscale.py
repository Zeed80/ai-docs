"""Улучшение грубого листа перед оцифровкой: когда включается и когда отказывает."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw

from app.ai.cad_recognize import sheet_upscale


def _sheet(line: int, size: tuple[int, int] = (1400, 1000)) -> np.ndarray:
    """Вал из прямоугольников и размерные подписи — линии заданной толщины."""
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    width, height = size
    axis = height // 2
    for x0, x1, r in ((150, 450, 120), (450, 800, 80), (800, 1150, 100)):
        draw.rectangle([x0, axis - r, x1, axis + r], outline=0, width=line)
    for i, text in enumerate(("12", "30", "35", "80", "Ø40", "Ø25")):
        draw.text((180 + 150 * i, 120), text, fill=0)
        draw.line([(160 + 150 * i, 140), (260 + 150 * i, 140)], fill=0, width=max(1, line // 2))
    return np.asarray(image)


def test_the_main_line_measure_tells_a_sharp_sheet_from_a_coarse_one():
    assert sheet_upscale.main_line_px(_sheet(6)) >= 4.5
    assert sheet_upscale.main_line_px(_sheet(2)) < 4.5


def test_the_factor_follows_the_line_and_keeps_the_output_bounded():
    assert sheet_upscale.upscale_factor(6.0, (2480, 3508), 4.5) == 0  # чёткий лист
    assert sheet_upscale.upscale_factor(1.5, (620, 877), 4.5) == 4  # 75 dpi
    assert sheet_upscale.upscale_factor(3.0, (827, 1169), 4.5) == 3  # 100 dpi
    # Большое фото с тонкими линиями — не больше MAX_SIDE_PX по длинной стороне.
    assert sheet_upscale.upscale_factor(2.0, (3000, 4000), 4.5) == 0
    assert sheet_upscale.upscale_factor(2.0, (1800, 2400), 4.5) == 3
    # Лист 600 px с линией 0,77 px (живой part_02): ×4 давало 2,7 px, ×7 — 4,44.
    assert sheet_upscale.upscale_factor(0.77, (425, 600), 4.5) == 8
    assert sheet_upscale.upscale_factor(0.3, (425, 600), 4.5) == 8


def test_an_honest_upscale_agrees_and_a_swapped_label_does_not():
    low = _sheet(2, size=(700, 500))
    honest = np.asarray(Image.fromarray(low).resize((2100, 1500), Image.LANCZOS))
    forged = honest.copy()
    # «Модель перерисовала подпись»: одна подпись заменена другой.
    forged[330:390, 520:640] = honest[330:390, 1420:1540]
    forged[120:200, 600:760] = 255
    forged[140:180, 620:740] = honest[140:180, 1060:1180]

    assert sheet_upscale.agreement(low, honest)["worst_tile"] >= sheet_upscale.MIN_TILE_AGREEMENT
    assert sheet_upscale.agreement(low, forged)["worst_tile"] < sheet_upscale.MIN_TILE_AGREEMENT


def test_texture_on_the_blank_field_and_at_the_frame_edge_is_not_a_disagreement():
    """Живой z4-r4: SeedVR2 кладёт текстуру на ровное поле за листом и иначе
    рисует край кадра — предохранитель отверг честный лист (худшая плитка 0,00)."""
    low = _sheet(2, size=(700, 500)).copy()
    low[:, 660:] = 200  # ровное поле за листом после выпрямления
    honest = np.asarray(Image.fromarray(low).resize((2100, 1500), Image.LANCZOS)).copy()
    rng = np.random.default_rng(0)
    honest[:, 1980:] = np.clip(200 + rng.normal(0, 18, honest[:, 1980:].shape), 0, 255)
    honest[:, -12:] = 0  # край кадра — другой тон

    assert sheet_upscale.agreement(low, honest)["worst_tile"] >= sheet_upscale.MIN_TILE_AGREEMENT


def test_a_sharp_sheet_is_left_as_is():
    buffer = io.BytesIO()
    Image.fromarray(_sheet(6)).save(buffer, format="PNG")
    result = sheet_upscale.upscale_sheet(buffer.getvalue(), comfy_url="http://127.0.0.1:9")

    assert result.applied is False
    assert result.content == buffer.getvalue()
    assert "чёткий" in result.reason


def test_an_unreachable_comfyui_falls_back_to_the_source(monkeypatch):
    calls = []
    monkeypatch.setattr(sheet_upscale, "_vram_free", lambda comfy: None)
    monkeypatch.setattr("app.ai.gpu_lock.unload_comfyui", lambda: calls.append("free"))
    monkeypatch.setattr("app.ai.gpu_lock.unload_ollama", lambda: calls.append("ollama") or 0)
    buffer = io.BytesIO()
    Image.fromarray(_sheet(2)).save(buffer, format="PNG")
    result = sheet_upscale.upscale_sheet(
        buffer.getvalue(), comfy_url="http://127.0.0.1:9", timeout_s=5
    )

    assert result.applied is False
    assert result.content == buffer.getvalue()
    assert "апскейл недоступен" in result.reason
    assert result.factor >= 2
    assert calls == ["free"]  # ComfyUI отпущен и после отказа


def _fake_comfy(monkeypatch, image: np.ndarray) -> None:
    monkeypatch.setattr(sheet_upscale, "_vram_free", lambda comfy: None)
    monkeypatch.setattr("app.ai.gpu_lock.unload_comfyui", lambda: None)
    monkeypatch.setattr("app.ai.gpu_lock.unload_ollama", lambda: 0)

    def upscale(comfy, png, scale, *, timeout_s=600):
        buffer = io.BytesIO()
        Image.fromarray(image).save(buffer, format="PNG")
        return buffer.getvalue()

    monkeypatch.setattr(sheet_upscale, "run_comfy_upscale", upscale)


def _png(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_redrawn_label_is_replaced_by_the_plain_upscale_and_the_sheet_is_kept(monkeypatch):
    """Живой part_01: SeedVR2 перерисовал дату в штампе — 2 плитки из 16 802, и
    отвергался весь лист. Теперь эти плитки — простым увеличением."""
    low = _sheet(2)
    factor = sheet_upscale.upscale_factor(sheet_upscale.main_line_px(low), low.shape, 4.5)
    size = (low.shape[1] * factor, low.shape[0] * factor)
    honest = np.asarray(Image.fromarray(low).resize(size, Image.LANCZOS))
    forged = honest.copy()
    # Подпись «12» заменена подписью «80» — как перерисованная цифра.
    forged[100 * factor : 135 * factor, 175 * factor : 210 * factor] = honest[
        100 * factor : 135 * factor, 625 * factor : 660 * factor
    ]
    _fake_comfy(monkeypatch, forged)

    result = sheet_upscale.upscale_sheet(_png(low), comfy_url="http://comfy")

    assert result.applied is True, result.reason
    assert "простое увеличение" in result.reason
    assert result.agreement["patched_tiles"] >= 1
    kept = np.asarray(Image.open(io.BytesIO(result.content)).convert("L")).astype(float)
    region = (slice(100 * factor, 135 * factor), slice(175 * factor, 210 * factor))
    # Подделки на месте подписи больше нет — там простое увеличение исходника.
    assert np.abs(kept[region] - honest[region].astype(float)).mean() < 8.0


def test_a_sheet_that_disagrees_everywhere_is_not_taken(monkeypatch):
    low = _sheet(2)
    factor = sheet_upscale.upscale_factor(sheet_upscale.main_line_px(low), low.shape, 4.5)
    rng = np.random.default_rng(1)
    noise = rng.integers(0, 255, (low.shape[0] * factor, low.shape[1] * factor), dtype=np.uint8)
    _fake_comfy(monkeypatch, noise)

    result = sheet_upscale.upscale_sheet(_png(low), comfy_url="http://comfy")

    assert result.applied is False
    assert "расходится" in result.reason
