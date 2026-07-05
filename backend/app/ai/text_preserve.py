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
    image_bytes: bytes,
    lang: str = "rus+eng",
    min_conf: float = 35.0,
    include_rotated: bool = True,
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

    ``include_rotated`` adds a second pass over the image rotated 90° —
    tesseract cannot see vertical text at all, and ЕСКД drawings are full of
    it (vertical dimensions like ⌀46, frame-column labels). Boxes from that
    pass are mapped back into the original orientation; a horizontal-text
    false hit in the rotated pass just fails ``min_conf`` like any noise.

    Returns ``[]`` (never raises) if pytesseract/tesseract isn't available —
    callers treat an empty list as "nothing to preserve", not an error.
    """
    try:
        import pytesseract  # noqa: F401
        from PIL import Image
    except ImportError:
        logger.debug("text_preserve_ocr_unavailable")
        return []

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        regions = _ocr_pass(img, lang, min_conf)
        if include_rotated:
            # ЕСКД vertical text (frame columns, vertical dimensions like ⌀46)
            # reads bottom-to-top, i.e. it sits 90° CCW from horizontal — a
            # clockwise rotation (PIL ROTATE_270 = 270° CCW) makes it readable.
            # Mapping back: rotated (xr, yr, wr, hr) → original
            # (x=yr, y=H-xr-wr, w=hr, h=wr).
            rotated = img.transpose(Image.Transpose.ROTATE_270)
            h_orig = img.height
            for r in _ocr_pass(rotated, lang, min_conf):
                regions.append(
                    TextRegion(
                        text=r.text,
                        x=r.y, y=h_orig - r.x - r.w, w=r.h, h=r.w,
                        conf=r.conf,
                    )
                )
        return regions
    except Exception as exc:  # noqa: BLE001 — OCR must never break generation
        logger.warning("text_preserve_ocr_failed", error=str(exc))
        return []


def _ocr_pass(img: "Image.Image", lang: str, min_conf: float) -> list[TextRegion]:
    import cv2
    import numpy as np
    import pytesseract
    from PIL import Image

    gray = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2GRAY)
    gray = cv2.resize(gray, None, fx=_OCR_UPSCALE, fy=_OCR_UPSCALE, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ocr_input = Image.fromarray(binary)
    # psm 12: sparse text + OSD — CAD drawings scatter short labels around
    # geometry rather than dense paragraphs, unlike tesseract's psm 3 default.
    data = pytesseract.image_to_data(
        ocr_input, lang=lang, config="--psm 12", output_type=pytesseract.Output.DICT
    )

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


def _extract_ink_alpha(
    crop_rgb: "Image.Image", dark_threshold: int = 235, binarize_ink: bool = False
):
    """RGBA crop where dark ink strokes are opaque, near-white background is
    fully transparent — so compositing only paints the actual glyphs, not a
    rectangle, regardless of what's behind them in the diffusion output.

    The glyphs are sharpened before alpha extraction and the alpha itself is
    contrast-boosted: source text usually comes from a phone photo whose soft,
    grayish strokes would otherwise sit visibly washed-out on the crisp white
    background the cleanup pipeline produces. ``binarize_ink`` additionally
    paints the strokes pure black (alpha still anti-aliased) — for pasting
    onto a binarized cleanup result, where gray photo-ink would look dirty.
    All of these only change stroke rendering (darker, better-defined edges)
    — never glyph shapes, so a dimension value cannot be altered by them."""
    import numpy as np
    from PIL import Image, ImageFilter

    sharpened = crop_rgb.filter(ImageFilter.UnsharpMask(radius=2, percent=120, threshold=2))
    gray = sharpened.convert("L")
    arr = np.asarray(gray).astype(np.float32)
    if binarize_ink:
        # The fixed threshold below assumes near-white paper. A phone photo's
        # background sits well under it (~190 after CLAHE), which painted the
        # WHOLE box as a translucent black veil on the first live run — use
        # the crop's own Otsu split instead, with a short ramp for smooth
        # glyph edges: strokes fully opaque, photo background fully clear.
        import cv2

        otsu_thr, _ = cv2.threshold(
            arr.astype("uint8"), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        alpha = np.clip((float(otsu_thr) - arr) / 25.0 * 255.0, 0, 255).astype("uint8")
        rgba = Image.new("RGBA", sharpened.size, (0, 0, 0, 255))
    else:
        alpha = np.clip((dark_threshold - arr) / dark_threshold * 255.0 * 1.6, 0, 255).astype("uint8")
        alpha[arr > dark_threshold] = 0
        rgba = sharpened.convert("RGBA")
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


def _upscale_crop(crop_rgb: "Image.Image", factor: int = 2):
    """Deterministic super-resolution of a text crop before pasting: photo
    glyphs at 8-15px are the sharpness bottleneck of the whole hybrid result.
    Lanczos ×2 + edge-aware unsharp visibly crispens strokes and CANNOT
    change what the text says (unlike a learned SR model, this is a pure
    resampling filter — the safe default; an ESRGAN pass can slot in here
    later behind the same interface)."""
    from PIL import Image, ImageFilter

    up = crop_rgb.resize((crop_rgb.width * factor, crop_rgb.height * factor), Image.LANCZOS)
    return up.filter(ImageFilter.UnsharpMask(radius=2, percent=140, threshold=2))


_REDUNDANT_RECALL = 0.60  # crop ink covered by output ink ≥ this → already drawn
# 4px: residual local drift after affine aiming reaches ~5px on live v2
# output; at 2px half the true duplicates slipped through. Genuine text is
# still safe — the model's pseudo-glyphs miss real glyph strokes by far more
# than the dilation covers.
_REDUNDANT_DILATE_PX = 4


def _already_drawn(out_rgba: "Image.Image", dest: tuple[int, int], crop_rgb: "Image.Image") -> bool:
    """True when the output already contains the crop's ink structure at the
    paste location. Measured as recall of the crop's ink under the (slightly
    dilated) output ink. Line structures the model redrew (window frames,
    hatching) match almost stroke-for-stroke → high recall; real text vs the
    model's pseudo-glyphs does not. Best-effort: any failure means "paste as
    before" — the filter must never lose genuine text to an exception."""
    try:
        import cv2
        import numpy as np

        w, h = crop_rgb.size
        region = out_rgba.crop((dest[0], dest[1], dest[0] + w, dest[1] + h)).convert("L")
        out_gray = np.asarray(region)
        crop_gray = np.asarray(crop_rgb.convert("L"))

        # Fixed thresholds, not Otsu: a mostly-white crop degenerates Otsu to
        # 0 (confirmed by test). 128 catches photo ink after CLAHE; the 160
        # fallback covers washed-out strokes.
        crop_ink = crop_gray < 128
        if crop_ink.sum() < 30:
            crop_ink = crop_gray < 160
        if crop_ink.sum() < 30:  # nothing substantial to compare
            return False
        out_ink = (out_gray < 128).astype(np.uint8)
        k = 2 * _REDUNDANT_DILATE_PX + 1
        out_grown = cv2.dilate(out_ink, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))) > 0
        recall = float((crop_ink & out_grown).sum()) / float(crop_ink.sum())
        if recall >= _REDUNDANT_RECALL:
            logger.debug("text_paste_skipped_already_drawn", recall=round(recall, 2))
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


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
    binarize_ink: bool = False,
    source_to_result=None,
) -> bytes:
    """Paste the original (ink-only, alpha-masked) text back onto the
    diffusion result at proportionally-mapped locations.

    ``source_w``/``source_h`` are the dimensions of the image ``regions`` was
    computed against (the same image whose bytes are ``source_bytes``) — the
    diffusion output may be a different resolution; boxes are scaled, not
    assumed pixel-identical.

    ``binarize_ink``: paste pure-black glyphs on a pure-white backing patch
    instead of the source photo's own gray ink/background — for cleanup
    results, whose background is crisp white after binarization; the photo's
    gray tones would otherwise sit on it as visibly dirty rectangles.

    ``source_to_result``: optional 2x3 affine matrix (image_align.
    estimate_source_to_result) mapping source pixel coordinates to the
    result's layout (in source-resolution units). Used to AIM each paste when
    diffusion re-laid-out the sheet, instead of warping the clean result onto
    the source photo's residual tilt (which turned straight windows into
    parallelograms — confirmed live).
    """
    from PIL import Image

    out = Image.open(io.BytesIO(diffusion_png)).convert("RGBA")
    src = Image.open(io.BytesIO(source_bytes)).convert("RGB")
    ow, oh = out.size
    if source_w <= 0 or source_h <= 0:
        return diffusion_png
    sx, sy = ow / source_w, oh / source_h

    for r in regions:
        # A real dimension/label line is SHORT vertically and small relative
        # to the sheet. OCR on a photo also produces occasional huge false
        # "lines" (a door/window pattern read as characters, words grouped
        # across half the sheet) — pasting those drops a big chunk of raw
        # photo onto the clean result (confirmed live). Size-gate them out.
        if r.h > 0.08 * source_h or (r.w * r.h) > 0.05 * (source_w * source_h):
            continue
        pad = max(1, int(max(r.w, r.h) * pad_frac))
        x0, y0 = max(0, r.x - pad), max(0, r.y - pad)
        x1, y1 = min(source_w, r.x + r.w + pad), min(source_h, r.y + r.h + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        crop = _upscale_crop(src.crop((x0, y0, x1, y1)))
        if not _looks_like_text(crop):
            # OCR false positive (e.g. a leader-line arrowhead or a colored
            # graphic element mistaken for a text line) — pasting it back
            # verbatim would introduce a visible, wrongly-placed patch.
            # Real dimension/label text on ЕСКД/GOST drawings is always
            # near-monochrome; skip anything that isn't, leaving whatever
            # the diffusion pass drew there untouched (safe no-op).
            continue
        # Aim the paste: proportional mapping by default; through the affine
        # layout matrix when the diffusion output's layout drifted.
        if source_to_result is not None:
            m = source_to_result
            cx_src, cy_src = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            cx = m[0][0] * cx_src + m[0][1] * cy_src + m[0][2]
            cy = m[1][0] * cx_src + m[1][1] * cy_src + m[1][2]
            box_scale_x = (m[0][0] ** 2 + m[1][0] ** 2) ** 0.5
            box_scale_y = (m[0][1] ** 2 + m[1][1] ** 2) ** 0.5
            target_w = max(1, round((x1 - x0) * box_scale_x * sx))
            target_h = max(1, round((y1 - y0) * box_scale_y * sy))
            dest = (round(cx * sx - target_w / 2), round(cy * sy - target_h / 2))
        else:
            target_w, target_h = max(1, round((x1 - x0) * sx)), max(1, round((y1 - y0) * sy))
            dest = (round(x0 * sx), round(y0 * sy))
        # Clamp into the canvas: a transformed target can poke past an edge,
        # and PIL's alpha_composite requires a non-negative dest.
        if target_w > ow or target_h > oh:
            continue
        dest = (max(0, min(dest[0], ow - target_w)), max(0, min(dest[1], oh - target_h)))
        crop = crop.resize((target_w, target_h), Image.LANCZOS)
        if _already_drawn(out, dest, crop):
            # The diffusion output already contains this exact structure —
            # window frames / hatching fragments that OCR mistook for text.
            # Pasting the photo's (thicker) ink over the model's clean thin
            # strokes reads as a double exposure (confirmed live on the v2
            # LoRA output). Genuine text is NOT skipped by this: the model
            # draws pseudo-glyph mush there, which never matches the real
            # glyph pixels stroke-for-stroke.
            continue
        # Paint over whatever the diffusion pass drew in this region first —
        # otherwise its own (garbled) text attempt shows through/around the
        # crisp glyphs pasted on top, producing a "ghosting"/double-exposure
        # look. Backing color = the source's own background near the text
        # (nearly white on a real drawing), not a fixed white, so it still
        # blends with a slightly toned/aged scan.
        bg = _WHITE if binarize_ink else _dominant_light_color(crop)
        patch = Image.new("RGBA", crop.size, (*bg, 255))
        out.alpha_composite(patch, dest=dest)

        rgba = _extract_ink_alpha(crop, binarize_ink=binarize_ink)
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
