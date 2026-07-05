"""Dataset preparation core for the studio's "Обучение LoRA" tab.

Production port of the research pipeline in tools/lora-dataset/ (kept there
as the standalone research CLI; THIS module is what the app runs). One
prepared pair is:

    target  = clean render (DXF/DWG via ezdxf, PNG passthrough, or synthetic
              ЕСКД sheets from techdraw)
    control = exact-unwarp(simulate_photo(target)) + CLAHE

Alignment contract (learned the hard way — see project memory
project-lora-v2-and-align): control and target MUST be pixel-aligned; the
control is unwarped with the ground-truth corners recorded during photo
simulation, never with a detected quad. A systematic layout offset in the
pairs teaches the LoRA to re-layout sheets and breaks the cleanup pipeline's
proportional text paste.

Captioning uses a caller-chosen vision model. Confidentiality: real customer
drawings must be captioned by LOCAL models only — the caller passes the
model; cloud models are only legitimate for synthetic-only datasets.
"""

from __future__ import annotations

import base64
import io
import json
import pathlib
import random
import re
import shutil
import subprocess
import tempfile
import urllib.request

import structlog

logger = structlog.get_logger()

CAPTION_PROMPT = (
    "Ты описываешь чертёж для датасета. Ответь по-русски, 1-2 коротких предложения, "
    "строго по содержимому: тип (чертёж детали / сборочный чертёж / план / фасад / "
    "разрез / схема), состав видов, ключевые элементы. "
    "БЕЗ размеров, БЕЗ номеров документов, БЕЗ оценок качества."
)
DEFAULT_INSTRUCTION = (
    "convert this into a clean black and white technical line drawing, "
    "crisp sharp uniform lines, remove background, remove noise and shadows, "
    "remove binding, white background. Содержимое чертежа: {caption}"
)
_REFUSAL_MARKERS = ("не могу", "невозможно", "cannot", "unable", "извини")
_CAPTION_MAX_PX = 800  # qwen3.6:35b OOMs the 24GB card above this (measured)


# ── Targets ──────────────────────────────────────────────────────────────────


def looks_like_clean_drawing(img) -> bool:
    """Cleanliness gate for raster uploads: a TARGET must already be a clean
    drawing (white paper + sparse dark ink). The most likely user error is
    uploading a PHOTO of a drawing — the pipeline would then degrade the
    photo and teach the model that dirt is the ground truth. A clean render
    is strongly bimodal; a desk photo has a broad mid-tone background."""
    import numpy as np

    arr = np.asarray(img.convert("L"))
    white = float((arr > 200).mean())
    background = float(np.median(arr))
    return white >= 0.5 and background >= 180


def render_target(path: pathlib.Path, out_png: pathlib.Path,
                  long_side: int = 2048) -> str | None:
    """One source file → one clean PNG target. PNG/JPG pass through
    (rescaled, cleanliness-gated); DXF renders via ezdxf; DWG needs the
    dwg2dxf binary (LibreDWG). Returns a rejection reason, or None on
    success — callers surface the reason in the dataset stats."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".png", ".jpg", ".jpeg"):
            from PIL import Image

            img = Image.open(path).convert("RGB")
            if not looks_like_clean_drawing(img):
                return ("источник похож на фото, а не на чистый чертёж — "
                        "таргетом должен быть чистый рендер/скан")
            img.thumbnail((long_side, long_side))
            img.save(out_png)
            return None
        if suffix == ".dwg":
            if shutil.which("dwg2dxf") is None:
                logger.warning("lora_dataset_no_dwg2dxf", file=path.name)
                return "конвертер dwg2dxf не установлен"
            with tempfile.TemporaryDirectory() as tmp:
                dxf = pathlib.Path(tmp) / (path.stem + ".dxf")
                subprocess.run(
                    ["dwg2dxf", "-y", "-o", str(dxf), str(path)],
                    capture_output=True, timeout=300,
                )
                if not dxf.exists():
                    return "dwg2dxf не смог сконвертировать файл"
                return None if _render_dxf(dxf, out_png, long_side) else "ошибка рендера DXF"
        if suffix == ".dxf":
            return None if _render_dxf(path, out_png, long_side) else "ошибка рендера DXF"
        return f"неподдерживаемый формат {suffix}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("lora_dataset_render_failed", file=path.name, error=str(exc)[:160])
        return str(exc)[:160]


def _render_dxf(dxf_path: pathlib.Path, out_png: pathlib.Path, long_side: int) -> bool:
    import matplotlib

    matplotlib.use("Agg")
    import ezdxf
    import matplotlib.pyplot as plt
    from ezdxf import bbox
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
    from ezdxf.tools.text import plain_mtext

    doc = ezdxf.readfile(dxf_path)
    layouts = [doc.modelspace()] + [doc.blocks.get(b.name) for b in doc.blocks]
    for layout in layouts:
        if layout is None:
            continue
        # Both repairs below target dwg2dxf conversion artifacts (см. tools/
        # lora-dataset/render_dwg.py): dangling anonymous-block INSERTs and
        # MTEXT inline font codes that render as tofu without the named font.
        for ins in list(layout.query("INSERT")):
            if ins.dxf.name not in doc.blocks:
                layout.delete_entity(ins)
        for e in layout.query("MTEXT"):
            if re.search(r"\\f[^;]*;", e.text):
                e.text = plain_mtext(e.text)

    msp = doc.modelspace()
    try:
        extents = bbox.extents(msp, fast=True)
        ratio = (extents.size.y / extents.size.x) if extents.has_data and extents.size.x else 0.7
    except Exception:  # noqa: BLE001
        ratio = 0.7
    ratio = min(max(ratio, 0.1), 10.0)
    dpi = 150
    if ratio <= 1.0:
        figsize = (long_side / dpi, long_side * ratio / dpi)
    else:
        figsize = (long_side / ratio / dpi, long_side / dpi)

    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    fig = plt.figure(figsize=figsize, dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    Frontend(RenderContext(doc), MatplotlibBackend(ax), config=cfg).draw_layout(msp, finalize=True)
    fig.savefig(out_png, dpi=dpi, facecolor="white", bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return True


def _is_drawing_page(gray) -> bool:
    """Cheap text-page filter for scanned albums: a drawing album carries
    covers, tables of contents and text sheets, and training a CLEANUP model
    on them dilutes the dataset. Text pages show a regular row structure:
    the per-row ink profile alternates dense/empty stripes; drawings spread
    ink irregularly across the sheet."""
    import numpy as np

    ink = gray < 128
    frac = float(ink.mean())
    if frac < 0.003:  # nearly blank page (back cover, separator)
        return False
    rows = ink.mean(axis=1)
    active = rows > max(0.01, frac * 0.3)
    if not active.any():
        return False
    # Count alternation runs of the active-row mask; normalize by height.
    switches = int(np.abs(np.diff(active.astype(np.int8))).sum())
    stripe_rate = switches / len(rows)
    # Text pages: many short uniform stripes (high switch rate) AND ink
    # concentrated in them; drawings: few long irregular regions.
    return not (stripe_rate > 0.08 and frac < 0.12)


def render_pdf_targets(path: pathlib.Path, out_dir: pathlib.Path,
                       long_side: int = 1024, max_pages: int = 400) -> tuple[int, int]:
    """A multi-page PDF album → one target per page; returns (rendered,
    skipped_non_drawing). Built for scanned drawing albums (e.g. the
    182-page ТЭМ2 locomotive album): pages are raster scans, so each render
    gets a gentle background normalization (levels to white paper) WITHOUT
    binarizing the linework — the target must stay a natural clean drawing,
    not a thresholded mask. Resumable: existing page files are kept."""
    import fitz
    import numpy as np
    from PIL import Image

    doc = fitz.open(path)
    ok = 0
    skipped = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for page_no in range(min(doc.page_count, max_pages)):
        out_png = out_dir / f"{path.stem}_p{page_no:03d}.png"
        if out_png.exists():
            ok += 1
            continue
        try:
            page = doc[page_no]
            scale = long_side / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            arr = np.asarray(img).astype(np.float32)
            gray = arr.mean(axis=2)
            if not _is_drawing_page(gray):
                skipped += 1
                continue
            # Normalize paper to white, measured on the page BORDER (outer
            # 10% is reliably paper; a dense hatched drawing drags a
            # full-frame percentile down). The gain is clamped: an
            # unclamped 255/paper on a dark scan blows faint pencil lines
            # straight to white.
            h, w = gray.shape
            bh, bw = max(1, h // 10), max(1, w // 10)
            border = np.concatenate([
                gray[:bh].ravel(), gray[-bh:].ravel(),
                gray[:, :bw].ravel(), gray[:, -bw:].ravel(),
            ])
            paper = float(np.percentile(border, 85))
            if paper < 250:
                gain = min(255.0 / max(paper, 1.0), 1.3)
                arr = np.clip(arr * gain, 0, 255)
            Image.fromarray(arr.astype("uint8")).save(out_png)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_dataset_pdf_page_failed", page=page_no, error=str(exc)[:120])
    doc.close()
    return ok, skipped


def generate_synthetic_targets(out_dir: pathlib.Path, count: int, seed: int = 42,
                               long_side: int = 1024) -> int:
    """Random ЕСКД sheets (shafts/plates/assemblies) via the project's own
    deterministic renderer — unlimited non-confidential targets.

    ``long_side`` should MATCH the training resolution (v3 lesson): rendering
    at 2048 and training at 768 pushed every thick line through a downscale
    that turns it into two soft edges — the model then literally learned to
    draw thick lines as doubled thin ones. Rendering is done at a randomized
    scale and resized to ``long_side`` so line pixel-widths vary naturally."""
    import cairosvg
    from PIL import Image

    from app.ai.techdraw import render_spec_to_svg

    from app.ai import lora_synth_specs as specs

    rng = random.Random(seed)
    ok = 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        # The rng draws stay in lockstep across resumes: specs are generated
        # even for images we skip, so re-running with the same seed yields
        # the same sequence.
        kind = rng.choices(["shaft", "plate", "assembly"], weights=[4, 4, 2])[0]
        spec = specs.random_spec(kind, rng)
        render_side = int(long_side * rng.uniform(0.85, 1.35))
        out_png = out_dir / f"synth_{kind}_{i:04d}.png"
        if out_png.exists():
            ok += 1
            continue
        try:
            svg = render_spec_to_svg(spec)
            png = cairosvg.svg2png(bytestring=svg.encode(), output_width=render_side,
                                   background_color="white")
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if max(img.size) != long_side:
                scale = long_side / max(img.size)
                img = img.resize((round(img.width * scale), round(img.height * scale)),
                                 Image.LANCZOS)
            img.save(out_png)
            # The spec is the ground truth — captions for synthetics are
            # built from it deterministically (no VLM hours, no VLM errors).
            out_png.with_suffix(".spec.json").write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_dataset_synth_failed", kind=kind, i=i, error=str(exc)[:120])
    return ok


def _title_font(size: int):
    """A REAL scalable font for the title block. PIL's default bitmap font
    is a ~10px fly speck on a 1024px sheet — nothing like the readable
    printed text the model must learn to preserve. DejaVuSans ships with
    matplotlib (already a dependency) and covers Cyrillic."""
    from PIL import ImageFont

    try:
        import matplotlib

        ttf = pathlib.Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf"
        return ImageFont.truetype(str(ttf), size)
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def wrap_in_eskd_sheet(png_path: pathlib.Path, title: dict | None = None) -> None:
    """Put an arbitrary rendered drawing onto a ГОСТ-styled sheet in place:
    frame (20mm left / 5mm other margins by convention) + a simplified form-1
    title block. DWG renders come frameless (modelspace only) — but real
    photos are of PRINTED sheets, so targets must carry the sheet furniture
    for the model to learn to preserve it. The block is filled with readable
    text (real sheets never carry an empty stamp)."""
    import zlib as _zlib

    from PIL import Image, ImageDraw

    img = Image.open(png_path).convert("RGB")
    w, h = img.size
    mm = max(w, h) / 420.0  # treat the long side as an A3 landscape (420mm)
    left, other = round(20 * mm), round(5 * mm)
    frame_w = max(2, round(0.7 * mm))

    sheet = Image.new("RGB", (w + left + other + 2 * frame_w, h + 2 * other + 2 * frame_w),
                      "white")
    sheet.paste(img, (left + frame_w, other + frame_w))
    d = ImageDraw.Draw(sheet)
    sw, sh = sheet.size
    d.rectangle([left, other, sw - other, sh - other], outline="black", width=frame_w)

    # Simplified form-1 title block, bottom-right inside the frame.
    tb_w, tb_h = round(185 * mm * 0.6), round(55 * mm * 0.6)
    x1, y1 = sw - other - frame_w, sh - other - frame_w
    x0, y0 = x1 - tb_w, y1 - tb_h
    d.rectangle([x0, y0, x1, y1], outline="black", width=frame_w)
    col = x0 + round(tb_w * 0.35)
    d.line([col, y0, col, y1], fill="black", width=max(1, frame_w // 2))
    for frac in (0.33, 0.66):
        d.line([x0, y0 + tb_h * frac, x1, y0 + tb_h * frac], fill="black",
               width=max(1, frame_w // 2))

    t = dict(title or {})
    name = str(t.get("name") or "Чертёж")[:40]
    if not t.get("designation"):
        # Deterministic plausible ГОСТ designation from the name.
        code = _zlib.crc32(name.encode("utf-8"))
        t["designation"] = f"ТМ.{100000 + code % 900000}.{1 + code % 999:03d}"
    if not t.get("material"):
        t["material"] = "Сталь 45 ГОСТ 1050-88"
    font = _title_font(max(10, round(3.5 * mm)))
    pad = max(4, round(1.5 * mm))
    d.text((x0 + pad, y0 + pad), t["designation"], fill="black", font=font)
    d.text((col + pad, y0 + tb_h * 0.36 + pad), name, fill="black", font=font)
    d.text((x0 + pad, y0 + tb_h * 0.66 + pad), t["material"], fill="black", font=font)
    sheet.save(png_path)


def generate_edit_pairs(targets_dir: pathlib.Path, controls_dir: pathlib.Path,
                        count: int, seed: int = 42, long_side: int = 1536) -> int:
    """"drawing_edit" preset: pairs (render(A) → render(A')) with the exact
    RU edit instruction as the training prompt. No photo degradation and no
    VLM captions — the instruction IS the label, and both renders share the
    layout by construction."""
    import cairosvg
    from PIL import Image

    from app.ai import lora_synth_specs as specs
    from app.ai.techdraw import render_spec_to_svg

    rng = random.Random(seed)
    targets_dir.mkdir(parents=True, exist_ok=True)
    controls_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    attempts = 0
    while ok < count and attempts < count * 3:
        attempts += 1
        kind = rng.choices(["shaft", "plate"], weights=[3, 2])[0]
        spec = specs.random_spec(kind, rng)
        mutated = specs.mutate_spec(spec, rng)
        if not mutated:
            continue
        spec2, instruction = mutated
        try:
            name = f"edit_{kind}_{ok:04d}__v0"
            if (targets_dir / f"{name}.png").exists() and (controls_dir / f"{name}.png").exists():
                ok += 1  # resumed: pair already rendered and validated
                continue

            def _render(s: dict) -> "Image.Image":
                png = cairosvg.svg2png(bytestring=render_spec_to_svg(s).encode(),
                                       output_width=long_side, background_color="white")
                return Image.open(io.BytesIO(png)).convert("RGB")

            img_a, img_b = _render(spec), _render(spec2)
            if img_a.size != img_b.size:
                continue  # mutation changed extents/layout → not a valid pair
            import numpy as np

            diff_px = int((np.abs(
                np.asarray(img_a.convert("L"), dtype=np.int16)
                - np.asarray(img_b.convert("L"), dtype=np.int16)
            ) > 50).sum())
            if diff_px < max(60, 0.08 * max(img_a.size)):
                # Drop invisible edits (a chamfer removal on a tiny segment
                # changes ~0 px — measured min was 0). The threshold scales
                # LINEARLY with the long side, not with area: a CAD edit is a
                # thin outline whose pixel count grows ~linearly with
                # resolution, so an area fraction over-rejects at high res.
                continue
            img_b.save(targets_dir / f"{name}.png")   # target = AFTER the edit
            img_a.save(controls_dir / f"{name}.png")  # control = BEFORE
            (targets_dir / f"{name}.txt").write_text(instruction, encoding="utf-8")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("lora_dataset_edit_pair_failed", error=str(exc)[:120])
    return ok


# ── Degradation (photo simulation, v2 exact alignment) ──────────────────────


def degrade_target(clean_png: pathlib.Path, out_png: pathlib.Path, seed: int) -> bool:
    import cv2
    import numpy as np
    from PIL import Image

    from app.ai import lora_degrade as deg

    try:
        clean = np.asarray(Image.open(clean_png).convert("RGB"))
        h, w = clean.shape[:2]
        rng = np.random.default_rng(seed)
        photo, quad = deg.simulate_photo(clean, rng)
        control = deg.unwarp_exact(photo, quad, w, h, rng)
        control = deg.post_unwarp_defects(control, rng)
        control = deg.clahe_like_prod(control)
        Image.fromarray(control).save(out_png)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("lora_dataset_degrade_failed", file=clean_png.name, error=str(exc)[:120])
        return False


# ── Captioning ───────────────────────────────────────────────────────────────


def caption_image(image_path: pathlib.Path, model: str, ollama_url: str,
                  fallback_model: str | None = None) -> str | None:
    """Short RU content caption via a local vision model. Downscale ladder
    (800→640) works around the vision-encoder OOM on 24GB — for the fallback
    model too (it can OOM at 800px just like the primary); refusals and
    trivial answers are rejected (QA)."""
    for candidate, max_px in ((model, _CAPTION_MAX_PX), (model, 640),
                              (fallback_model, _CAPTION_MAX_PX), (fallback_model, 640)):
        if not candidate:
            continue
        try:
            text = _ollama_caption(ollama_url, candidate, image_path, max_px)
        except Exception as exc:  # noqa: BLE001
            logger.info("lora_caption_attempt_failed", model=candidate, error=str(exc)[:100])
            continue
        cleaned = " ".join(text.split())
        if len(cleaned) >= 25 and not any(m in cleaned.lower() for m in _REFUSAL_MARKERS):
            return cleaned
    return None


def _ollama_caption(ollama_url: str, model: str, image_path: pathlib.Path, max_px: int) -> str:
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    img.thumbnail((max_px, max_px))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    body = {
        "model": model,
        "prompt": CAPTION_PROMPT,
        "images": [base64.b64encode(buf.getvalue()).decode()],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 200},
    }
    req = urllib.request.Request(
        f"{ollama_url}/api/generate",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=600).read())
    return (resp.get("response") or "").strip()


# ── Assembly + QA ────────────────────────────────────────────────────────────


def build_pair(target_png: pathlib.Path, control_png: pathlib.Path, caption: str,
               images_dir: pathlib.Path, control_dir: pathlib.Path, name: str,
               instruction: str = DEFAULT_INSTRUCTION) -> str | None:
    """QA one (target, control, caption) triple and place it into the
    ai-toolkit layout. Returns a rejection reason, or None when accepted."""
    ink = _ink_fraction(target_png)
    if not 0.005 <= ink <= 0.35:
        return f"target ink fraction {ink:.3f}"
    if _ink_fraction(control_png) < 0.001:
        return "control is blank"
    ratio = _aspect(control_png) / _aspect(target_png)
    if not 0.95 <= ratio <= 1.05:
        # Tight on purpose: a systematic aspect/layout mismatch is exactly
        # what taught the v1 LoRA to re-layout sheets.
        return f"aspect mismatch {ratio:.2f}"

    images_dir.mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(target_png, images_dir / f"{name}.png")
    shutil.copyfile(control_png, control_dir / f"{name}.png")
    (images_dir / f"{name}.txt").write_text(
        instruction.format(caption=caption), encoding="utf-8"
    )
    return None


def _ink_fraction(path: pathlib.Path) -> float:
    import numpy as np
    from PIL import Image

    img = np.asarray(Image.open(path).convert("L"))
    return float((img < 128).mean())


def _aspect(path: pathlib.Path) -> float:
    from PIL import Image

    with Image.open(path) as img:
        return img.width / img.height


def make_preview(control_png: pathlib.Path, target_png: pathlib.Path, max_h: int = 480) -> bytes:
    """Side-by-side (control | target) JPEG for the UI dataset card."""
    from PIL import Image

    c = Image.open(control_png).convert("RGB")
    t = Image.open(target_png).convert("RGB")
    c = c.resize((int(c.width * max_h / c.height), max_h))
    t = t.resize((int(t.width * max_h / t.height), max_h))
    combo = Image.new("RGB", (c.width + t.width + 12, max_h), (128, 128, 128))
    combo.paste(c, (0, 0))
    combo.paste(t, (c.width + 12, 0))
    buf = io.BytesIO()
    combo.save(buf, format="JPEG", quality=85)
    return buf.getvalue()
