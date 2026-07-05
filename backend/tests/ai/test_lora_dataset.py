"""Unit tests for the LoRA dataset core (lora_dataset / lora_degrade)."""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

pytest.importorskip("cv2")

import cv2  # noqa: E402
from PIL import Image  # noqa: E402


def _drawing(w=600, h=450) -> np.ndarray:
    img = np.full((h, w, 3), 255, np.uint8)
    cv2.rectangle(img, (60, 50), (520, 380), (0, 0, 0), 2)
    cv2.circle(img, (180, 120), 40, (0, 0, 0), 2)
    cv2.line(img, (60, 200), (520, 200), (0, 0, 0), 2)
    return img


def test_degrade_pairs_are_pixel_aligned(tmp_path: pathlib.Path):
    """The v2 contract: control unwarps with ground-truth corners, so the
    residual shift vs the target is only the random jitter (a few px), never
    a systematic layout offset (the v1 failure mode)."""
    from app.ai import lora_degrade as deg

    clean = _drawing()
    h, w = clean.shape[:2]
    rng = np.random.default_rng(7)
    photo, quad = deg.simulate_photo(clean, rng)
    control = deg.unwarp_exact(photo, quad, w, h, rng)

    t_ink = (cv2.cvtColor(clean, cv2.COLOR_RGB2GRAY) < 128).astype(np.float32)
    c_ink = (cv2.cvtColor(control, cv2.COLOR_RGB2GRAY) < 100).astype(np.float32)
    (dx, dy), _ = cv2.phaseCorrelate(t_ink, c_ink)
    assert abs(dx) < 5 and abs(dy) < 5, f"misaligned: dx={dx:.1f} dy={dy:.1f}"


def test_build_pair_rejects_aspect_mismatch(tmp_path: pathlib.Path):
    from app.ai import lora_dataset as core

    target = tmp_path / "t.png"
    control = tmp_path / "c.png"
    Image.fromarray(_drawing(600, 450)).save(target)
    Image.fromarray(_drawing(600, 320)).save(control)  # squashed layout

    reason = core.build_pair(target, control, "чертёж", tmp_path / "images",
                             tmp_path / "control", "pair0")
    assert reason and "aspect" in reason


def test_build_pair_accepts_good_pair_and_writes_prompt(tmp_path: pathlib.Path):
    from app.ai import lora_dataset as core

    target = tmp_path / "t.png"
    control = tmp_path / "c.png"
    Image.fromarray(_drawing()).save(target)
    Image.fromarray(_drawing()).save(control)

    reason = core.build_pair(target, control, "фасад здания", tmp_path / "images",
                             tmp_path / "control", "pair0")
    assert reason is None
    prompt = (tmp_path / "images" / "pair0.txt").read_text(encoding="utf-8")
    assert "фасад здания" in prompt and "clean black and white" in prompt


def test_render_target_passthrough_png(tmp_path: pathlib.Path):
    from app.ai import lora_dataset as core

    src = tmp_path / "src.png"
    Image.fromarray(_drawing()).save(src)
    out = tmp_path / "out.png"
    assert core.render_target(src, out) is None  # None = accepted
    assert out.exists()


def test_render_target_rejects_photo_like_png(tmp_path: pathlib.Path):
    """Cleanliness gate: a mid-tone photo (no white paper) is refused so the
    pipeline never degrades a photo into a "clean target"."""
    from app.ai import lora_dataset as core

    rng = np.random.default_rng(0)
    photo = rng.integers(60, 170, (400, 500, 3), dtype=np.uint8)  # broad mid-tones
    src = tmp_path / "photo.jpg"
    Image.fromarray(photo).save(src)
    reason = core.render_target(src, tmp_path / "out.png")
    assert reason and "фото" in reason


def test_synthetic_targets_render(tmp_path: pathlib.Path):
    pytest.importorskip("cairosvg")
    from app.ai import lora_dataset as core

    n = core.generate_synthetic_targets(tmp_path, count=3, seed=1, long_side=800)
    assert n == 3
    files = list(tmp_path.glob("synth_*.png"))
    assert len(files) == 3
    for f in files:
        with Image.open(f) as img:
            assert max(img.size) == 800


def test_post_unwarp_defects_stay_local(tmp_path: pathlib.Path):
    """v3 defects (residual trapezoid / curl / crease) must be LOCAL
    distortions the model learns to fix — never a global layout shift that
    would re-teach the v1 re-layout failure."""
    from app.ai import lora_degrade as deg

    clean = _drawing()
    rng = np.random.default_rng(3)
    defected = deg.post_unwarp_defects(clean.copy(), rng)
    t_ink = (cv2.cvtColor(clean, cv2.COLOR_RGB2GRAY) < 128).astype(np.float32)
    d_ink = (cv2.cvtColor(defected, cv2.COLOR_RGB2GRAY) < 128).astype(np.float32)
    (dx, dy), _ = cv2.phaseCorrelate(t_ink, d_ink)
    assert abs(dx) < 4 and abs(dy) < 4, f"global shift too big: {dx:.1f},{dy:.1f}"


def test_edit_pairs_generation(tmp_path: pathlib.Path):
    pytest.importorskip("cairosvg")
    from app.ai import lora_dataset as core

    targets = tmp_path / "t"
    controls = tmp_path / "c"
    n = core.generate_edit_pairs(targets, controls, count=3, seed=5, long_side=800)
    assert n == 3
    for t in targets.glob("*.png"):
        name = t.stem
        assert (controls / f"{name}.png").exists()
        instr = (targets / f"{name}.txt").read_text(encoding="utf-8")
        assert any(w in instr for w in ("фаск", "резьб", "отверст", "расточк"))
        # Пара должна отличаться (правка реально видна)...
        a = np.asarray(Image.open(controls / f"{name}.png").convert("L"), dtype=np.int16)
        b = np.asarray(Image.open(t).convert("L"), dtype=np.int16)
        assert (np.abs(a - b) > 50).sum() > 200  # правка видна (сотни px)
        # ...но совпадать по компоновке (тот же лист).
        assert a.shape == b.shape


def test_wrap_in_eskd_sheet(tmp_path: pathlib.Path):
    from app.ai import lora_dataset as core

    p = tmp_path / "bare.png"
    Image.fromarray(_drawing()).save(p)
    core.wrap_in_eskd_sheet(p, {"name": "Тест", "designation": "ТМ.1", "material": "Ст3"})
    img = np.asarray(Image.open(p).convert("L"))
    h, w = img.shape
    # Рамка: в приграничных полосах есть сплошные тёмные линии.
    assert ((img[:, : int(w * 0.1)] < 128).sum(axis=0)).max() > h * 0.5  # левая
    assert ((img[: int(h * 0.1), :] < 128).sum(axis=1)).max() > w * 0.5  # верхняя
    # Штамп: плотность линий в правом нижнем углу.
    corner = img[int(h * 0.85):, int(w * 0.55):]
    assert (corner < 128).mean() > 0.005


def test_pdf_album_renders_pages(tmp_path: pathlib.Path):
    fitz = pytest.importorskip("fitz")
    from app.ai import lora_dataset as core

    pdf = tmp_path / "album.pdf"
    doc = fitz.open()
    for i in range(3):
        page = doc.new_page(width=400, height=300)
        page.draw_rect(fitz.Rect(50, 50, 350, 250), color=(0, 0, 0), width=2)
        page.insert_text((60, 70), f"list {i}")
    doc.save(pdf)
    doc.close()

    out = tmp_path / "targets"
    rendered, skipped = core.render_pdf_targets(pdf, out, long_side=800)
    assert rendered == 3
    pages = sorted(out.glob("album_p*.png"))
    assert len(pages) == 3
    with Image.open(pages[0]) as img:
        assert max(img.size) == 800
    # Resumable: a second call keeps the existing pages, renders nothing new.
    rendered2, _ = core.render_pdf_targets(pdf, out, long_side=800)
    assert rendered2 == 3


def test_degrade_is_deterministic_by_seed(tmp_path: pathlib.Path):
    """Same seed → identical control. Reproducibility was silently broken by
    hash(); the pipeline now feeds a fixed integer seed to degrade_target."""
    from app.ai import lora_dataset as core

    target = tmp_path / "t.png"
    Image.fromarray(_drawing()).save(target)
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    assert core.degrade_target(target, a, seed=12345)
    assert core.degrade_target(target, b, seed=12345)
    assert np.array_equal(np.asarray(Image.open(a)), np.asarray(Image.open(b)))


def test_spec_caption_is_deterministic_and_relevant():
    import random

    from app.ai import lora_synth_specs as specs

    for kind in ("shaft", "plate", "assembly"):
        spec = specs.random_spec(kind, random.Random(3))
        cap = specs.spec_caption(spec)
        assert specs.spec_caption(spec) == cap  # pure function
        assert len(cap) >= 25 and "надпись" in cap


def test_holdout_split_is_stable():
    """Name-keyed holdout: the same pair name always lands in the same split
    (so resumed preparations don't reshuffle train/holdout)."""
    from app.tasks.lora_training import _is_holdout

    names = [f"synth_shaft_{i:04d}__v0" for i in range(200)]
    first = {n: _is_holdout(n) for n in names}
    assert all(_is_holdout(n) == first[n] for n in names)
    frac = sum(first.values()) / len(names)
    assert 0.02 < frac < 0.25  # ~1/10, not everything or nothing


def test_synthetic_targets_write_spec_json(tmp_path: pathlib.Path):
    pytest.importorskip("cairosvg")
    from app.ai import lora_dataset as core

    core.generate_synthetic_targets(tmp_path, count=2, seed=9, long_side=800)
    specs = list(tmp_path.glob("synth_*.spec.json"))
    assert len(specs) == 2  # captions come from these, not a VLM
