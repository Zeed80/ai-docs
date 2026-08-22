"""Product pictures from a catalog page.

Three sources, because suppliers draw their goods in three different ways:
embedded rasters (photos), inline images, and VECTOR line art — machine-tool
catalogs draw the tool with paths, and a `get_images()`-only implementation
would leave most positions with no picture at all.

Coordinate convention (the single most important rule here): every bbox that
leaves this module is in PIXELS OF THE SAVED PAGE RASTER. PDF points, raster
pixels and tesseract's own pixels are three different systems, and mixing them
shifts every crop by a silent amount. `pdf_bbox_to_raster` is the only bridge.
"""

from __future__ import annotations

import hashlib
import io
import math
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger()

# A candidate smaller than this is a bullet, a rule or an icon, not a product.
MIN_SIDE_PX = 40
MIN_AREA_RATIO = 0.008
MAX_ASPECT = 10.0
# A picture repeated on this share of pages is furniture (logo, header, footer).
REPEAT_SHARE_THRESHOLD = 0.30
REPEAT_MIN_PAGES = 10
# Below this the article and the picture are not convincingly related.
MATCH_SCORE_THRESHOLD = 0.55


@dataclass
class ImageCandidate:
    """A picture found on a page, in raster pixels."""

    key: str
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 in raster px
    source: str  # "raster" | "inline" | "vector"
    signature: str = ""  # for the repeated-furniture filter
    path: str | None = None
    thumb_path: str | None = None

    @property
    def width(self) -> int:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> int:
        return self.bbox[3] - self.bbox[1]

    @property
    def center(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.key,
            "bbox": list(self.bbox),
            "source": self.source,
            "signature": self.signature,
            "path": self.path,
            "thumb_path": self.thumb_path,
            "w": self.width,
            "h": self.height,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ImageCandidate":
        bbox = tuple(int(v) for v in (raw.get("bbox") or [0, 0, 0, 0]))  # type: ignore[assignment]
        return cls(
            key=str(raw.get("k") or ""),
            bbox=bbox,  # type: ignore[arg-type]
            source=str(raw.get("source") or "raster"),
            signature=str(raw.get("signature") or ""),
            path=raw.get("path"),
            thumb_path=raw.get("thumb_path"),
        )


@dataclass
class WordBox:
    """A word with its box, already in raster pixels."""

    text: str
    bbox: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) / 2, (self.bbox[1] + self.bbox[3]) / 2)


@dataclass
class ImageMatch:
    candidate: ImageCandidate | None
    score: float
    kind: str  # "crop" | "page"
    shared: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)


def pdf_bbox_to_raster(
    bbox: tuple[float, float, float, float], dpi: int
) -> tuple[int, int, int, int]:
    """PDF points (72/inch, origin top-left in PyMuPDF) → raster pixels."""
    zoom = dpi / 72.0
    return (
        int(round(bbox[0] * zoom)),
        int(round(bbox[1] * zoom)),
        int(round(bbox[2] * zoom)),
        int(round(bbox[3] * zoom)),
    )


def _acceptable(bbox: tuple[int, int, int, int], raster: tuple[int, int]) -> bool:
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if width < MIN_SIDE_PX or height < MIN_SIDE_PX:
        return False
    page_area = max(raster[0] * raster[1], 1)
    if (width * height) / page_area < MIN_AREA_RATIO:
        return False
    aspect = width / max(height, 1)
    if aspect > MAX_ASPECT or aspect < 1 / MAX_ASPECT:
        return False
    # A "picture" covering the whole page is the page, not a product.
    return (width * height) / page_area <= 0.85


def _merge_boxes(
    boxes: list[tuple[float, float, float, float]], gap: float
) -> list[tuple[float, float, float, float]]:
    """Cluster nearby vector paths into one illustration."""
    merged: list[list[float]] = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        placed = False
        for current in merged:
            overlap_x = box[0] <= current[2] + gap and current[0] <= box[2] + gap
            overlap_y = box[1] <= current[3] + gap and current[1] <= box[3] + gap
            if overlap_x and overlap_y:
                current[0] = min(current[0], box[0])
                current[1] = min(current[1], box[1])
                current[2] = max(current[2], box[2])
                current[3] = max(current[3], box[3])
                placed = True
                break
        if not placed:
            merged.append(list(box))
    return [tuple(box) for box in merged]  # type: ignore[misc]


def extract_page_image_candidates(
    page: Any, doc: Any, raster_size: tuple[int, int], dpi: int
) -> list[ImageCandidate]:
    """Find every plausible product picture on one page.

    `page`/`doc` are PyMuPDF objects; the caller already rendered the page, so
    everything here is geometry plus a signature for the furniture filter.
    """
    candidates: list[ImageCandidate] = []
    seen_boxes: set[tuple[int, int, int, int]] = set()

    def _add(bbox_pt, source: str, signature: str = "") -> None:
        bbox = pdf_bbox_to_raster(tuple(bbox_pt), dpi)
        bbox = (
            max(0, bbox[0]),
            max(0, bbox[1]),
            min(raster_size[0], bbox[2]),
            min(raster_size[1], bbox[3]),
        )
        if bbox in seen_boxes or not _acceptable(bbox, raster_size):
            return
        seen_boxes.add(bbox)
        candidates.append(
            ImageCandidate(
                key=f"{source[0]}{len(candidates)}",
                bbox=bbox,
                source=source,
                signature=signature or _geometry_signature(bbox, raster_size),
            )
        )

    # 1. Embedded rasters — the xref gives stable bytes, so the signature is
    #    exact and the logo filter below becomes reliable.
    try:
        for xref, *_rest in page.get_images(full=True):
            signature = ""
            try:
                raw = doc.extract_image(xref)
                signature = hashlib.sha256(raw["image"][:4096]).hexdigest()[:16]
            except Exception:  # noqa: BLE001 — signature is an optimisation
                signature = f"xref:{xref}"
            for rect in page.get_image_rects(xref) or []:
                _add(rect, "raster", signature)
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalog_page_rasters_failed", error=str(exc)[:120])

    # 2. Inline images (type-1 blocks) — same picture may appear here instead.
    try:
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") == 1 and block.get("bbox"):
                _add(block["bbox"], "inline")
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalog_page_inline_failed", error=str(exc)[:120])

    # 3. Vector line art — a machine-tool catalog draws the tool with paths;
    #    without this branch most positions would fall back to a page preview.
    try:
        drawing_boxes = [
            tuple(d["rect"]) for d in page.get_drawings() if d.get("rect") is not None
        ]
        if drawing_boxes:
            gap = 8.0  # points; parts of one drawing sit close together
            for box in _merge_boxes(drawing_boxes, gap):
                _add(box, "vector")
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalog_page_vectors_failed", error=str(exc)[:120])

    return candidates


def _geometry_signature(bbox: tuple[int, int, int, int], raster: tuple[int, int]) -> str:
    """Position+size fingerprint — a header logo lands in the same place."""
    rel = (
        round(bbox[0] / max(raster[0], 1), 2),
        round(bbox[1] / max(raster[1], 1), 2),
        round((bbox[2] - bbox[0]) / max(raster[0], 1), 2),
        round((bbox[3] - bbox[1]) / max(raster[1], 1), 2),
    )
    return "g:" + ":".join(str(v) for v in rel)


def furniture_signatures(pages_images: list[list[dict]]) -> set[str]:
    """Signatures that repeat across pages — logos, headers, page frames."""
    total_pages = len(pages_images)
    if total_pages < REPEAT_MIN_PAGES:
        return set()
    counts: dict[str, int] = {}
    for images in pages_images:
        for signature in {img.get("signature") for img in images if img.get("signature")}:
            counts[str(signature)] = counts.get(str(signature), 0) + 1
    threshold = max(REPEAT_MIN_PAGES // 2, int(total_pages * REPEAT_SHARE_THRESHOLD))
    return {signature for signature, count in counts.items() if count >= threshold}


def normalize_code(value: str | None) -> str:
    if not value:
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def find_code_box(words: list[WordBox], code: str) -> tuple[int, int, int, int] | None:
    """Locate an article code among page words, tolerating split tokens."""
    target = normalize_code(code)
    if len(target) < 3:
        return None
    for word in words:
        if normalize_code(word.text) == target:
            return word.bbox
    # Codes get broken across words ("MT190", "-", "016C04")
    for index, word in enumerate(words):
        merged = normalize_code(word.text)
        if not merged or not target.startswith(merged):
            continue
        bbox = list(word.bbox)
        for follower in words[index + 1 : index + 5]:
            merged += normalize_code(follower.text)
            bbox[0] = min(bbox[0], follower.bbox[0])
            bbox[1] = min(bbox[1], follower.bbox[1])
            bbox[2] = max(bbox[2], follower.bbox[2])
            bbox[3] = max(bbox[3], follower.bbox[3])
            if merged == target:
                return (bbox[0], bbox[1], bbox[2], bbox[3])
            if not target.startswith(merged):
                break
    return None


def _score_candidate(
    code_bbox: tuple[int, int, int, int],
    candidate: ImageCandidate,
    raster: tuple[int, int],
) -> float:
    code_cx, code_cy = (code_bbox[0] + code_bbox[2]) / 2, (code_bbox[1] + code_bbox[3]) / 2
    img_cx, img_cy = candidate.center

    diagonal = math.hypot(raster[0], raster[1]) or 1.0
    distance = math.hypot(code_cx - img_cx, code_cy - img_cy) / diagonal
    proximity = max(0.0, 1.0 - distance * 2.2)

    # Same row: their vertical ranges overlap (a table of variants under a
    # picture) — or same column: the picture sits directly above the code.
    row_overlap = min(code_bbox[3], candidate.bbox[3]) - max(code_bbox[1], candidate.bbox[1])
    row_band = 1.0 if row_overlap > 0 else 0.0
    col_overlap = min(code_bbox[2], candidate.bbox[2]) - max(code_bbox[0], candidate.bbox[0])
    column = 1.0 if col_overlap > 0 else 0.0

    return round(0.6 * proximity + 0.25 * row_band + 0.15 * column, 3)


def match_entries_to_images(
    entries: list[dict[str, Any]],
    words: list[WordBox],
    candidates: list[ImageCandidate],
    raster: tuple[int, int],
) -> list[ImageMatch]:
    """Pick a picture for each extracted position on the page.

    Falls back to `kind="page"` rather than to nothing: the user asked for a
    picture on every position, and a page preview honestly labelled as such is
    more useful than an empty cell.
    """
    matches: list[ImageMatch] = []
    used: dict[str, int] = {}

    for entry in entries:
        code_bbox = None
        for key in ("part_number", "name"):
            code_bbox = find_code_box(words, str(entry.get(key) or ""))
            if code_bbox:
                break

        best: ImageCandidate | None = None
        best_score = 0.0
        if code_bbox and candidates:
            for candidate in candidates:
                score = _score_candidate(code_bbox, candidate, raster)
                if score > best_score:
                    best, best_score = candidate, score

        if best is not None and best_score >= MATCH_SCORE_THRESHOLD:
            used[best.key] = used.get(best.key, 0) + 1
            matches.append(
                ImageMatch(
                    candidate=best,
                    score=best_score,
                    kind="crop",
                    shared=used[best.key] > 1,
                    diagnostics={"code_found": True},
                )
            )
        else:
            matches.append(
                ImageMatch(
                    candidate=None,
                    score=best_score,
                    kind="page",
                    diagnostics={"code_found": bool(code_bbox)},
                )
            )

    # Table pages: the article is assembled from separate cells ("1A1" + "150" +
    # "20"), so the composed code never appears as text and every position falls
    # back to a page preview — measured on a real catalog: 1 crop out of 68.
    # Two layouts can still be resolved honestly, both marked with a lower
    # confidence than a located article:
    unmatched = [match for match in matches if match.kind == "page"]
    if unmatched:
        sizable = _sizable(candidates, raster)
        if sizable and len(unmatched) == len(sizable):
            # As many pictures as positions → the page lists them in order.
            ordered = sorted(sizable, key=lambda c: (c.bbox[1], c.bbox[0]))
            for match, candidate in zip(unmatched, ordered):
                match.candidate = candidate
                match.kind = "crop"
                match.score = 0.45
                match.diagnostics["reason"] = "reading_order"
        elif sizable and len(sizable) <= 6 and len(unmatched) >= 2 * len(sizable):
            # Many variants, few illustrations → a variants table: the biggest
            # illustration is the picture of that family. Shared, never claimed
            # as a per-item photo.
            dominant = max(sizable, key=lambda c: c.width * c.height)
            for match in unmatched:
                match.candidate = dominant
                match.kind = "crop"
                match.shared = True
                match.score = 0.4
                match.diagnostics["reason"] = "variants_table"
    return matches


def _sizable(candidates: list[ImageCandidate], raster: tuple[int, int]) -> list[ImageCandidate]:
    page_area = max(raster[0] * raster[1], 1)
    return [
        candidate
        for candidate in candidates
        if (candidate.width * candidate.height) / page_area >= 0.02
    ]


def _dominant_candidate(
    candidates: list[ImageCandidate], raster: tuple[int, int]
) -> ImageCandidate | None:
    """The one illustration a table page is built around, if there is one."""
    page_area = max(raster[0] * raster[1], 1)
    sizable = _sizable(candidates, raster)
    if not sizable:
        return None
    sizable.sort(key=lambda c: c.width * c.height, reverse=True)
    largest = sizable[0]
    largest_ratio = (largest.width * largest.height) / page_area
    if largest_ratio < 0.03:
        return None
    if len(sizable) == 1:
        return largest
    # A page about one product family normally carries a photo AND a profile
    # drawing (measured: a wheel page had 787×787 and 761×509 — the strict
    # "twice as large" rule rejected both and left 67 of 68 positions without a
    # picture). Few illustrations still mean one subject; many mean a catalogue
    # spread where guessing would attach the wrong product.
    second = sizable[1]
    if len(sizable) <= 4:
        return largest
    if (largest.width * largest.height) >= 2.0 * (second.width * second.height):
        return largest
    return None


def crop_image(page_png: bytes, bbox: tuple[int, int, int, int], *, pad: int = 6) -> bytes | None:
    """Cut a candidate out of the rendered page (raster pixels)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(page_png)) as image:
            box = (
                max(0, bbox[0] - pad),
                max(0, bbox[1] - pad),
                min(image.width, bbox[2] + pad),
                min(image.height, bbox[3] + pad),
            )
            if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                return None
            crop = image.crop(box).convert("RGB")
            buffer = io.BytesIO()
            crop.save(buffer, format="WEBP", quality=82, method=4)
            return buffer.getvalue()
    except Exception as exc:  # noqa: BLE001 — a bad crop must not fail the page
        logger.warning("catalog_crop_failed", error=str(exc)[:150])
        return None
