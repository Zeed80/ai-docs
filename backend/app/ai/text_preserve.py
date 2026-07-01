"""OCR-anchored text preservation for diffusion edit/cleanup — hybrid pipeline.

Diffusion (this project's Qwen-Image-Edit included) cannot render existing
dimension/tolerance/label text faithfully on a source drawing — it garbles it
on every pass, with or without ControlNet (confirmed by a direct live
experiment: see project memory ``project_studio_controlnet_experiment``).
Published work on this exact failure mode (OCR-guided inpainting, e.g.
"SA-OcrPaint") does not fix the model — it stops asking the model to touch
text at all: detect real text regions in the source *before* generation,
then paste the original ink back onto the diffusion output afterwards. This
module implements that as a deterministic post-processing step: the model
still does the (photo-)cleanup/style work, but every legible piece of text
that was already correct in the source stays correct in the result.

Resolution-independent: `composite_text_regions` maps source bounding boxes
into the output image's coordinate space proportionally, so it doesn't need
to know the diffusion pipeline's internal resize/pad behaviour.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()


@dataclass(frozen=True)
class TextRegion:
    text: str
    x: int  # px, in the SOURCE image's own coordinate space
    y: int
    w: int
    h: int
    conf: float  # tesseract confidence, 0-100


_OCR_UPSCALE = 3  # CAD dimension text is small relative to sheet size — tesseract
                   # misses most of it without upscaling first.


def detect_text_regions(
    image_bytes: bytes, lang: str = "rus+eng", min_conf: float = 35.0
) -> list[TextRegion]:
    """OCR the source image; return line-level bounding boxes (word boxes
    merged by tesseract's own line grouping) above ``min_conf``, in the
    ORIGINAL image's coordinate space (already divided back by the internal
    upscale).

    Only the LOCATION needs to be right for this module's purpose (pasting
    the original pixels back) — the recognized string content can be wrong
    (dense CAD drawings with hatching/GD&T symbols confuse tesseract's
    character recognition badly) without affecting the result, since
    ``composite_text_regions`` never uses ``.text``, only the bounding box.

    Returns ``[]`` (never raises) if pytesseract/tesseract isn't available —
    callers treat an empty list as "nothing to preserve", not an error.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.debug("text_preserve_ocr_unavailable")
        return []

    try:
        import cv2
        import numpy as np

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        gray = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
        gray = cv2.resize(gray, None, fx=_OCR_UPSCALE, fy=_OCR_UPSCALE, interpolation=cv2.INTER_CUBIC)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        ocr_input = Image.fromarray(binary)
        # psm 12: sparse text + OSD — CAD drawings scatter short labels around
        # geometry rather than dense paragraphs, unlike tesseract's psm 3 default.
        data = pytesseract.image_to_data(
            ocr_input, lang=lang, config="--psm 12", output_type=pytesseract.Output.DICT
        )
    except Exception as exc:  # noqa: BLE001 — OCR must never break generation
        logger.warning("text_preserve_ocr_failed", error=str(exc))
        return []

    lines: dict[tuple[int, int, int], dict] = {}
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not text or conf < min_conf:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        line = lines.setdefault(key, {"texts": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h, "confs": []})
        line["texts"].append(text)
        line["x0"] = min(line["x0"], x)
        line["y0"] = min(line["y0"], y)
        line["x1"] = max(line["x1"], x + w)
        line["y1"] = max(line["y1"], y + h)
        line["confs"].append(conf)

    s = _OCR_UPSCALE
    return [
        TextRegion(
            text=" ".join(line["texts"]),
            x=round(line["x0"] / s), y=round(line["y0"] / s),
            w=round((line["x1"] - line["x0"]) / s), h=round((line["y1"] - line["y0"]) / s),
            conf=sum(line["confs"]) / len(line["confs"]),
        )
        for line in lines.values()
    ]


def _extract_ink_alpha(crop_rgb: "Image.Image", dark_threshold: int = 235):
    """RGBA crop where dark ink strokes are opaque, near-white background is
    fully transparent — so compositing only paints the actual glyphs, not a
    rectangle, regardless of what's behind them in the diffusion output."""
    import numpy as np
    from PIL import Image

    gray = crop_rgb.convert("L")
    arr = np.asarray(gray).astype(np.float32)
    alpha = np.clip((dark_threshold - arr) / dark_threshold * 255.0, 0, 255).astype("uint8")
    alpha[arr > dark_threshold] = 0
    rgba = crop_rgb.convert("RGBA")
    rgba.putalpha(Image.fromarray(alpha))
    return rgba


_WHITE = (255, 255, 255)
_MIN_BACKGROUND_BRIGHTNESS = 200  # below this, trust plain white instead (see below)
_MAX_TEXT_SATURATION = 45  # 0-255; real dimension/label ink is near-monochrome
_MAX_DARK_FRACTION = 0.35  # text is sparse strokes on white — not a solid dark fill


def _looks_like_text(crop_rgb: "Image.Image") -> bool:
    """Reject OCR false positives that aren't actually text lines.

    Two independent tells catch different failure modes seen on real
    drawings: a strongly colored region (a red accent line) is almost always
    a misdetection — ЕСКД/GOST label ink is black/dark gray. A region that's
    mostly dark isn't text either, even if achromatic — real text is sparse
    strokes on a mostly-white background; a solidly dark crop is usually
    cross-hatching/shading from a 3D illustration that OCR mistook for a
    text line (observed on exploded-view drawings specifically).
    """
    import numpy as np

    arr = np.asarray(crop_rgb.convert("RGB"))
    hsv = np.asarray(crop_rgb.convert("HSV"))
    mean_saturation = float(hsv[..., 1].mean())
    if mean_saturation > _MAX_TEXT_SATURATION:
        return False
    gray = arr.mean(axis=2)
    dark_fraction = float((gray < 128).mean())
    return dark_fraction <= _MAX_DARK_FRACTION


def _dominant_light_color(crop_rgb: "Image.Image") -> tuple[int, int, int]:
    """Median color among the crop's lighter half — a robust stand-in for
    "the paper/background color behind this text" on a lightly toned/aged
    scan, ignoring the dark ink pixels themselves (which would otherwise pull
    a plain average too dark).

    Falls back to pure white when the result isn't actually light: an OCR
    bounding box that happens to overlap mostly dark linework (e.g. a label
    sitting across a thick object outline in an exploded-view illustration)
    would otherwise compute a dark "background" and paint a visible black
    block instead of blending in — pure white is the much safer default for
    ЕСКД/GOST drawings, which are white-background by convention.
    """
    import numpy as np

    arr = np.asarray(crop_rgb.convert("RGB")).reshape(-1, 3)
    brightness = arr.mean(axis=1)
    threshold = np.percentile(brightness, 60)
    light = arr[brightness >= threshold]
    if len(light) == 0:
        return _WHITE
    color = tuple(int(v) for v in np.median(light, axis=0))
    if sum(color) / 3 < _MIN_BACKGROUND_BRIGHTNESS:
        return _WHITE
    return color


def composite_text_regions(
    diffusion_png: bytes,
    source_bytes: bytes,
    regions: list[TextRegion],
    source_w: int,
    source_h: int,
    pad_frac: float = 0.15,
) -> bytes:
    """Paste the original (ink-only, alpha-masked) text back onto the
    diffusion result at proportionally-mapped locations.

    ``source_w``/``source_h`` are the dimensions of the image ``regions`` was
    computed against (the same image whose bytes are ``source_bytes``) — the
    diffusion output may be a different resolution; boxes are scaled, not
    assumed pixel-identical.
    """
    from PIL import Image

    out = Image.open(io.BytesIO(diffusion_png)).convert("RGBA")
    src = Image.open(io.BytesIO(source_bytes)).convert("RGB")
    ow, oh = out.size
    if source_w <= 0 or source_h <= 0:
        return diffusion_png
    sx, sy = ow / source_w, oh / source_h

    for r in regions:
        pad = max(1, int(max(r.w, r.h) * pad_frac))
        x0, y0 = max(0, r.x - pad), max(0, r.y - pad)
        x1, y1 = min(source_w, r.x + r.w + pad), min(source_h, r.y + r.h + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        crop = src.crop((x0, y0, x1, y1))
        if not _looks_like_text(crop):
            # OCR false positive (e.g. a leader-line arrowhead or a colored
            # graphic element mistaken for a text line) — pasting it back
            # verbatim would introduce a visible, wrongly-placed patch.
            # Real dimension/label text on ЕСКД/GOST drawings is always
            # near-monochrome; skip anything that isn't, leaving whatever
            # the diffusion pass drew there untouched (safe no-op).
            continue
        target_w, target_h = max(1, round((x1 - x0) * sx)), max(1, round((y1 - y0) * sy))
        crop = crop.resize((target_w, target_h), Image.LANCZOS)

        dest = (round(x0 * sx), round(y0 * sy))
        # Paint over whatever the diffusion pass drew in this region first —
        # otherwise its own (garbled) text attempt shows through/around the
        # crisp glyphs pasted on top, producing a "ghosting"/double-exposure
        # look. Backing color = the source's own background near the text
        # (nearly white on a real drawing), not a fixed white, so it still
        # blends with a slightly toned/aged scan.
        bg = _dominant_light_color(crop)
        patch = Image.new("RGBA", crop.size, (*bg, 255))
        out.alpha_composite(patch, dest=dest)

        rgba = _extract_ink_alpha(crop)
        out.alpha_composite(rgba, dest=dest)

    buf = io.BytesIO()
    out.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def text_fidelity_score(source_bytes: bytes, result_bytes: bytes, regions: list[TextRegion] | None = None) -> dict:
    """Quantify how faithfully the result reproduces the source's text pixels.

    NOT an OCR-word comparison: tesseract's *character recognition* is
    unreliable on dense CAD drawings (confirmed — even OCR-ing the untouched
    source yields garbled strings), so comparing recognized word sets scores
    a perfect hybrid result as "worse" than pure diffusion whenever the OCR
    noise pattern merely shifts. What's actually measurable and meaningful is
    mean absolute pixel difference *within the OCR-located regions*, source
    vs result — this only requires OCR to find *where* text is (reliable),
    not read it (unreliable). Lower is better; 0 = pixel-identical crops.

    Pass a pre-computed ``regions`` (from ``detect_text_regions(source_bytes)``)
    to avoid re-running OCR when comparing several results against one source.
    """
    import numpy as np
    from PIL import Image

    if regions is None:
        regions = detect_text_regions(source_bytes)
    if not regions:
        return {"mean_abs_diff": None, "region_count": 0}

    src_img = Image.open(io.BytesIO(source_bytes)).convert("L")
    out_img = Image.open(io.BytesIO(result_bytes)).convert("L")
    sw, sh = src_img.size
    ow, oh = out_img.size
    sx, sy = ow / sw, oh / sh

    diffs: list[float] = []
    for r in regions:
        x0, y0 = round(r.x * sx), round(r.y * sy)
        x1, y1 = round((r.x + r.w) * sx), round((r.y + r.h) * sy)
        if x1 <= x0 or y1 <= y0:
            continue
        src_crop = src_img.crop((r.x, r.y, r.x + r.w, r.y + r.h)).resize((x1 - x0, y1 - y0))
        out_crop = out_img.crop((x0, y0, x1, y1))
        a = np.asarray(src_crop, dtype=np.float32)
        b = np.asarray(out_crop, dtype=np.float32)
        diffs.append(float(np.mean(np.abs(a - b))))

    if not diffs:
        return {"mean_abs_diff": None, "region_count": 0}
    return {
        "mean_abs_diff": sum(diffs) / len(diffs),
        "region_count": len(diffs),
        "max_abs_diff": max(diffs),
    }
