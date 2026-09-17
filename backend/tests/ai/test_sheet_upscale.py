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


def test_a_hollow_stroke_from_a_large_upscale_is_filled_and_text_keeps_its_holes():
    """Живой part_02 (×8): основная линия вышла двумя полосками с белым зазором."""
    image = np.full((60, 200), 255, np.uint8)
    image[20:23, 10:190] = 0  # полоска
    image[25:28, 10:190] = 0  # полоска, зазор 2 px
    image[40:56, 150:170] = 0  # «буква» с просветом 8 px
    image[44:52, 156:164] = 255

    filled = sheet_upscale.fill_hollow_strokes(image, 8)

    assert (filled[20:28, 20:180] < 128).all()  # штрих сплошной
    assert (filled[45:51, 157:163] > 128).all()  # просвет буквы цел
    assert (sheet_upscale.fill_hollow_strokes(image, 3) == image).all()  # ×3 — не трогаем


def test_a_comfyui_refusal_keeps_its_reason_and_a_one_off_failure_is_retried(monkeypatch):
    """Живая втулка part_03: «HTTP Error 400: Bad Request» без тела — причину не
    узнать; тот же лист минутой позже увеличился ×8 — разовый отказ стоил листу
    всей проверки («не измеримо» при линии 0,8 px)."""
    import urllib.error

    monkeypatch.setattr(sheet_upscale, "_vram_free", lambda comfy: None)
    monkeypatch.setattr(sheet_upscale, "_RETRY_PAUSE_S", 0.0)
    monkeypatch.setattr("app.ai.gpu_lock.unload_comfyui", lambda: None)
    monkeypatch.setattr("app.ai.gpu_lock.unload_ollama", lambda: 0)

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"node_errors": {"1": "x"}}')
        )

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    source = _png(_sheet(2))
    refused = sheet_upscale.upscale_sheet(source, comfy_url="http://comfy", timeout_s=5)

    assert refused.applied is False
    assert "HTTP 400 /upload/image" in refused.reason and "node_errors" in refused.reason

    calls = []

    def flaky(comfy, png, scale, *, timeout_s=600):
        calls.append(scale)
        if len(calls) == 1:
            raise RuntimeError("HTTP 400 /prompt: busy")
        image = Image.open(io.BytesIO(png)).convert("L")
        return _png(np.asarray(image.resize((image.width * scale, image.height * scale))))

    monkeypatch.setattr(sheet_upscale, "run_comfy_upscale", flaky)
    retried = sheet_upscale.upscale_sheet(source, comfy_url="http://comfy", timeout_s=5)

    assert len(calls) == 2
    assert retried.applied is True, retried.reason


# ── Настройки оператора ──────────────────────────────────────────────────────


def test_the_operator_caps_the_factor():
    """Лист 600 px с линией 0,77 px просит ×8; оператор ограничил ×4."""
    assert sheet_upscale.upscale_factor(0.77, (425, 600), 4.5, max_factor=4) == 4
    assert sheet_upscale.upscale_factor(1.5, (620, 877), 4.5, max_factor=3) == 3


def _options(params=None, config=None, env=True):
    return sheet_upscale.upscale_options(
        params or {}, config or {}, env_enabled=env, env_timeout_s=600.0
    )


def test_the_run_checkbox_wins_over_settings_and_settings_over_the_environment():
    assert _options(env=False).enabled is False
    assert _options(env=False).enabled_source == "environment"
    assert _options({}, {"cad_upscale_enabled": True}, env=False).enabled is True
    run_off = _options({"auto_upscale": False}, {"cad_upscale_enabled": True})
    assert (run_off.enabled, run_off.enabled_source) == (False, "run")
    run_on = _options({"auto_upscale": True}, {"cad_upscale_enabled": False}, env=False)
    assert (run_on.enabled, run_on.enabled_source) == (True, "run")


def test_stored_numbers_apply_only_inside_the_limits():
    options = _options(
        {},
        {
            "cad_upscale_min_line_px": 6.0,
            "cad_upscale_max_factor": 4,
            "cad_upscale_timeout_s": 900,
            "cad_upscale_min_agreement": 0.8,
        },
    )
    assert (options.min_line_px, options.max_factor, options.timeout_s, options.min_agreement) == (
        6.0,
        4,
        900.0,
        0.8,
    )
    # Порог согласия 0,1 пропускал бы подменённые подписи — берётся умолчание.
    broken = _options({}, {"cad_upscale_min_agreement": 0.1, "cad_upscale_max_factor": True})
    assert broken.min_agreement == sheet_upscale.MIN_TILE_AGREEMENT
    assert broken.max_factor == 8


def test_the_operator_agreement_threshold_decides_whether_the_upscale_is_kept(monkeypatch):
    """Строгий порог оператора отвергает лист, который проходит по умолчанию:
    шум по всему листу опускает согласие плиток до ~0,88 — ниже 0,92, выше 0,72."""
    low = _sheet(2)
    factor = sheet_upscale.upscale_factor(sheet_upscale.main_line_px(low), low.shape, 4.5)
    size = (low.shape[1] * factor, low.shape[0] * factor)
    honest = np.asarray(Image.fromarray(low).resize(size, Image.LANCZOS)).astype(float)
    rng = np.random.default_rng(1)
    noisy = np.clip(honest + rng.normal(0, 60, honest.shape), 0, 255).astype(np.uint8)
    _fake_comfy(monkeypatch, noisy)

    default = sheet_upscale.upscale_sheet(_png(low), comfy_url="http://x")
    strict = sheet_upscale.upscale_sheet(_png(low), comfy_url="http://x", min_agreement=0.92)

    assert default.applied is True
    assert strict.applied is False
    assert "< 0.92" in strict.reason
