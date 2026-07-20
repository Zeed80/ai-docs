"""VLM as a hypothesis manager for dimension/annotation text (Ф4.1).

Per the critique correction: a VLM reading a chertёzh annotation does not
get to write a fact — it proposes READINGS, each with a probability, and the
caller (cross-checks in ``cad_hypothesis.py``, ultimately a human in review)
decides which one is trustworthy. This module only produces
``Alternative`` candidates; it never sets ``assurance`` above ``inferred``
itself (enforced by ``cad_ir.assurance`` on write, not here).

Deliberately narrow scope vs. ``drawing_extractor.py``'s whole-sheet feature
extraction: one crop, one annotation, a handful of candidate readings — cheap
enough to run per uncertain OCR hit without blowing an aux-LLM budget the way
a full-sheet VLM pass would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

logger = structlog.get_logger()

if TYPE_CHECKING:
    from app.ai.router import AIRouter

# Hard cap on VLM crop reads per vectorize run — this is a batch Celery task,
# not a chat turn, so it has no access to the agent's aux_quality_budget;
# bound cost/latency directly instead.
MAX_CROP_READS_PER_RUN = 20

# Upper bound on VLM tiles for a whole-sheet text read (cost/latency guard).
MAX_TEXT_TILES_PER_RUN = 16

_SHEET_TEXT_PROMPT = """На изображении — фрагмент технического чертежа (ЕСКД).
Найди ВЕСЬ читаемый текст: надписи основной надписи, номера позиций, буквы и
цифры координатной сетки, обозначения, размерные числа, примечания.

Верни СТРОГО JSON-массив без markdown. Каждый элемент:
{"text": "строка ровно как на чертеже", "bbox": [x1, y1, x2, y2]}
где bbox — пиксельные координаты рамки текста В ЭТОМ изображении
(x1,y1 — левый верх, x2,y2 — правый низ). Одиночные буквы и цифры тоже включай.
Не выдумывай текст, которого нет. Если текста нет — верни []."""


def _parse_json_array(raw: str) -> list:
    """Extract a top-level JSON array of records from a VLM response.

    The shared ``_parse_json_response`` is object-shaped and collapses a JSON
    array, so grounding output (``[{...}, {...}]``) needs its own tolerant
    parse: strip markdown fences, then load the outermost ``[...]``.
    """
    import json
    import re

    text = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        pass
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, list) else []
        except (ValueError, TypeError):
            return []
    return []


async def read_sheet_text_entities(
    image_bytes: bytes,
    *,
    router: "AIRouter | None" = None,
    confidential: bool = True,
    tile_size: int = 1024,
    overlap: int = 160,
    max_tiles: int = MAX_TEXT_TILES_PER_RUN,
) -> list:
    """VLM-first whole-sheet text reader (replaces tesseract-gated detection).

    Tesseract cannot even *detect* isolated single glyphs or sub-10px title-
    block text, so a tesseract-first pipeline can never reach them — the VLM
    enrichment only ever re-reads what tesseract already found. A modern local
    VLM (qwen3-vl) reads those directly and grounds each read with a box. The
    sheet is tiled (small text survives), each tile upscaled for legibility,
    read with grounding, mapped back to sheet pixels and de-duplicated across
    tile overlaps. Confidential by default: local model only, never cloud.
    """
    import base64
    import io

    from PIL import Image

    from app.ai.cad_ir.schema import Point, SourceRegion, TextEntity
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router

    try:
        sheet = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:  # noqa: BLE001
        return []
    width, height = sheet.size
    step = max(1, tile_size - overlap)
    origins = [
        (x, y)
        for y in range(0, max(1, height - overlap), step)
        for x in range(0, max(1, width - overlap), step)
    ][:max_tiles]

    async def _read_tile(ox: int, oy: int) -> list:
        tx1, ty1 = min(width, ox + tile_size), min(height, oy + tile_size)
        tile_w, tile_h = tx1 - ox, ty1 - oy
        tile = sheet.crop((ox, oy, tx1, ty1)).convert("RGB")
        # Upscale so ~7px title text becomes legible. Grounding coordinates are
        # scale-invariant (normalized 0..1000), so upscaling does not affect the
        # coordinate mapping below.
        upscale = max(1.0, 1400 / max(tile.width, tile.height, 1))
        if upscale > 1.0:
            tile = tile.resize(
                (round(tile.width * upscale), round(tile.height * upscale)), Image.LANCZOS
            )
        buffer = io.BytesIO()
        tile.save(buffer, format="PNG")
        request = AIRequest(
            task=AITask.DRAWING_ANALYSIS_VLM,
            messages=[ChatMessage(role="user", content=_SHEET_TEXT_PROMPT)],
            images=[base64.b64encode(buffer.getvalue()).decode()],
            confidential=confidential,
            allow_cloud=False,
        )
        try:
            response = await router.run(request)
        except Exception as exc:  # noqa: BLE001 — one tile must not fail the run
            logger.warning("vlm_sheet_text_tile_failed", error=str(exc)[:160])
            return []
        parsed = _parse_json_array(response.text or "")
        if not parsed:
            return []
        out = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            box = item.get("bbox") or item.get("bbox_2d")
            if not text or not isinstance(box, (list, tuple)) or len(box) != 4:
                continue
            try:
                bx1, by1, bx2, by2 = (float(v) for v in box)
            except (TypeError, ValueError):
                continue
            if bx2 <= bx1 or by2 <= by1:
                continue
            # qwen3-vl grounds boxes in a per-axis 0..1000 normalized space, not
            # input pixels — map each axis by the tile's own dimension.
            sx1 = ox + (bx1 / 1000.0) * tile_w
            sy1 = oy + (by1 / 1000.0) * tile_h
            sx2 = ox + (bx2 / 1000.0) * tile_w
            sy2 = oy + (by2 / 1000.0) * tile_h
            out.append(
                TextEntity(
                    position=Point(x=sx1, y=sy2),  # baseline-left, matches DXF insert
                    text=text,
                    height=max(4.0, sy2 - sy1),
                    origin="vlm",
                    line_class="dim",
                    width_class="thin",
                    confidence=0.9,
                    source_region=SourceRegion(x0=sx1, y0=sy1, x1=sx2, y1=sy2),
                    evidence=["vlm:qwen-vl grounded"],
                )
            )
        return out

    import asyncio

    tiles = await asyncio.gather(*[_read_tile(ox, oy) for ox, oy in origins])
    entities = [e for tile in tiles for e in tile]
    _snap_text_to_ink(entities, sheet)
    return _dedup_sheet_text(entities)


def _snap_text_to_ink(entities: list, gray) -> None:
    """Tighten each VLM read onto the actual glyph ink (in place).

    The VLM reads the string reliably but grounds it only within ~1-2× text
    height. Once that is close, snapping to the source ink recovers an exact
    position: within a small window around the coarse box, keep only text-sized
    connected components (drop long strokes and large blobs — those are
    geometry) and take their tight bounding box as the glyph extent.
    """
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001 — snapping is best-effort
        return

    from app.ai.cad_ir.schema import Point, SourceRegion

    ink = (np.asarray(gray) < 128).astype(np.uint8)
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(ink, 8)
    for entity in entities:
        region = entity.source_region
        if region is None:
            continue
        text_h = max(entity.height, 6.0)
        # The residual VLM localization error (~30-60px) is roughly constant and
        # far exceeds a small glyph's height, so the search window must be
        # generous, not height-proportional-tiny; the line clustering below then
        # keeps only the correct text line inside it.
        pad = max(40.0, 2.5 * text_h)
        box_cx = (region.x0 + region.x1) / 2
        box_cy = (region.y0 + region.y1) / 2
        box_half_w = (region.x1 - region.x0) / 2
        glyphs: list[tuple[float, float, float, float, float]] = []
        for i in range(1, count):
            cx, cy = centroids[i]
            left, top, comp_w, comp_h, _area = stats[i]
            if comp_h > 2.5 * text_h or comp_w > 8.0 * text_h or comp_h < 0.35 * text_h:
                continue  # a stroke or a large blob, not a glyph
            if abs(cx - box_cx) > pad + box_half_w or abs(cy - box_cy) > pad:
                continue
            glyphs.append((float(left), float(top), float(comp_w), float(comp_h), float(cy)))
        if not glyphs:
            continue
        # Keep only the text line nearest the coarse box (drop glyphs from a
        # neighbouring row the wide window may have caught).
        line_cy = min(glyphs, key=lambda g: abs(g[4] - box_cy))[4]
        line = [g for g in glyphs if abs(g[4] - line_cy) <= 0.7 * text_h]
        xs = [g[0] for g in line] + [g[0] + g[2] for g in line]
        ys = [g[1] for g in line] + [g[1] + g[3] for g in line]
        # Reject a snap that inflates the box beyond a plausible glyph extent:
        # a single letter cannot be hundreds of px wide/tall — that means the
        # cluster swallowed nearby geometry. Keep the VLM box in that case so a
        # bad snap never yields a giant label (or, downstream, deletes lines).
        plausible_w = max(3.0, len((entity.text or "").strip())) * 1.6 * text_h
        if (max(xs) - min(xs)) > plausible_w or (max(ys) - min(ys)) > 2.2 * text_h:
            continue
        entity.position = Point(x=min(xs), y=max(ys))  # baseline-left of the ink
        entity.source_region = SourceRegion(
            x0=min(xs), y0=min(ys), x1=max(xs), y1=max(ys)
        )


def _dedup_sheet_text(entities: list) -> list:
    """Drop the same string read twice in a tile-overlap band (keep the first)."""
    import re

    kept: list = []
    for entity in entities:
        norm = re.sub(r"\s+", " ", (entity.text or "").strip()).casefold()
        height = max(entity.height, 6.0)
        duplicate = False
        for other in kept:
            other_norm = re.sub(r"\s+", " ", (other.text or "").strip()).casefold()
            if other_norm != norm:
                continue
            if (
                abs(entity.position.x - other.position.x) <= height
                and abs(entity.position.y - other.position.y) <= height
            ):
                duplicate = True
                break
        if not duplicate:
            kept.append(entity)
    return kept

_SYSTEM_PROMPT = """Ты читаешь МАЛЕНЬКИЙ вырезанный фрагмент технического чертежа —
одну размерную надпись, допуск, обозначение резьбы или шероховатости.
Изображение может быть нечётким, повёрнутым или частично обрезанным.

Верни СТРОГО JSON без markdown-блоков:
{
  "readings": [
    {"text": "Ø18H7", "value_mm": 18.0, "kind": "diameter", "tolerance": "H7", "confidence": 0.82},
    {"text": "Ø16H7", "value_mm": 16.0, "kind": "diameter", "tolerance": "H7", "confidence": 0.13},
    {"text": "M18", "value_mm": null, "kind": "thread", "tolerance": null, "confidence": 0.05}
  ]
}

Правила:
- kind: "diameter"|"linear"|"radius"|"angular"|"thread"|"roughness"|"tolerance_only"|"text"|"unclear"
- value_mm: числовое значение размера в мм, null если не размер (например резьба без диаметра-числа или произвольный текст)
- tolerance: буквенно-числовой допуск/квалитет (H7, h6, js6...) или null
- Если текст читается ОДНОЗНАЧНО — верни ОДИН элемент readings с confidence, близким к 1.0
- Если есть визуальная неоднозначность (спутать можно 3/8, 6/8/9, 1/7, H7/H2) —
  верни НЕСКОЛЬКО readings по убыванию confidence, их сумма confidence ≈ 1.0
- Если фрагмент нечитаем совсем — верни один readings с kind="unclear", confidence 0.0
- Не придумывай значения, которых не может быть на изображении — только то, что реально там может быть написано
"""


def _parse_response(raw_text: str) -> list[dict]:
    from app.ai.drawing_extractor import _parse_json_response

    parsed = _parse_json_response(raw_text)
    if not isinstance(parsed, dict):
        return []
    readings = parsed.get("readings")
    if not isinstance(readings, list):
        return []
    out = []
    for r in readings:
        if not isinstance(r, dict) or not r.get("text"):
            continue
        try:
            conf = float(r.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out.append({
            "text": str(r["text"]).strip(),
            "value_mm": _safe_float(r.get("value_mm")),
            "kind": r.get("kind") or "unclear",
            "tolerance": r.get("tolerance") or None,
            "confidence": max(0.0, min(1.0, conf)),
        })
    out.sort(key=lambda r: r["confidence"], reverse=True)
    return out


def _safe_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def read_crop_hypotheses(
    crop_png_bytes: bytes,
    *,
    router: "AIRouter | None" = None,
    confidential: bool = True,
) -> list[dict]:
    """One VLM call over one small crop -> ranked reading hypotheses.

    Never raises on model failure — returns ``[]`` (caller keeps the OCR
    reading as the sole, low-confidence hypothesis) so a flaky VLM never
    blocks the deterministic vectorize pipeline.
    """
    import base64

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router

    request = AIRequest(
        task=AITask.DRAWING_ANALYSIS_VLM,
        messages=[
            ChatMessage(role="system", content=_SYSTEM_PROMPT),
            ChatMessage(role="user", content="Прочитай этот фрагмент чертежа."),
        ],
        images=[base64.b64encode(crop_png_bytes).decode()],
        confidential=confidential,
        allow_cloud=False,
    )
    try:
        response = await router.run(request)
        readings = _parse_response(response.text or "")
        logger.info("vlm_crop_read", readings=len(readings))
        return readings
    except Exception as exc:  # noqa: BLE001 — a bad VLM call must not fail the pipeline
        logger.warning("vlm_crop_read_failed", error=str(exc)[:200])
        return []


_LINE_SYSTEM_PROMPT = """Ты смотришь на МАЛЕНЬКИЙ фрагмент технического чертежа с ОДНОЙ
выделенной линией (в центре кадра) и её ближайшим окружением.
Определи тип этой линии по ЕСКД (ГОСТ 2.303) и есть ли рядом инженерный символ
(шероховатость, резьба, сварной шов, база GD&T).

Верни СТРОГО JSON без markdown-блоков:
{
  "line_readings": [
    {"line_class": "axis", "confidence": 0.8},
    {"line_class": "hidden", "confidence": 0.2}
  ],
  "symbol": {"kind": "roughness", "text": "Ra 1.6", "confidence": 0.85}
}

line_class (выбери один или несколько по убыванию уверенности):
- "contour" — основная сплошная толстая (видимый контур детали)
- "thin" — тонкая сплошная (выносная, штриховка)
- "axis" — штрихпунктирная тонкая (осевая, центровая)
- "hidden" — штриховая (невидимый контур)
- "dim" — размерная линия (со стрелками)
symbol.kind: "roughness"|"thread"|"weld"|"datum"|"none" (используй "none" и confidence 0, если символа нет)
"""


def _parse_line_response(raw_text: str) -> dict:
    from app.ai.drawing_extractor import _parse_json_response

    parsed = _parse_json_response(raw_text)
    if not isinstance(parsed, dict):
        return {"line_readings": [], "symbol": None}
    readings = []
    for r in parsed.get("line_readings") or []:
        if not isinstance(r, dict) or not r.get("line_class"):
            continue
        try:
            conf = max(0.0, min(1.0, float(r.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        readings.append({"line_class": str(r["line_class"]), "confidence": conf})
    readings.sort(key=lambda r: r["confidence"], reverse=True)

    symbol = None
    raw_symbol = parsed.get("symbol")
    if isinstance(raw_symbol, dict) and raw_symbol.get("kind") not in (None, "none"):
        try:
            sconf = max(0.0, min(1.0, float(raw_symbol.get("confidence", 0.0))))
        except (TypeError, ValueError):
            sconf = 0.0
        if sconf > 0:
            symbol = {
                "kind": str(raw_symbol["kind"]),
                "text": raw_symbol.get("text") or "",
                "confidence": sconf,
            }
    return {"line_readings": readings, "symbol": symbol}


async def classify_line_hypotheses(
    crop_png_bytes: bytes,
    *,
    router: "AIRouter | None" = None,
    confidential: bool = True,
) -> dict:
    """One VLM call over a crop centered on an ambiguous line -> ranked
    line_class hypotheses + an optional detected engineering symbol.
    Degrades to an empty result on any failure — same non-blocking contract
    as ``read_crop_hypotheses``."""
    import base64

    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router

    request = AIRequest(
        task=AITask.DRAWING_ANALYSIS_VLM,
        messages=[
            ChatMessage(role="system", content=_LINE_SYSTEM_PROMPT),
            ChatMessage(role="user", content="Классифицируй выделенную линию."),
        ],
        images=[base64.b64encode(crop_png_bytes).decode()],
        confidential=confidential,
        allow_cloud=False,
    )
    try:
        response = await router.run(request)
        result = _parse_line_response(response.text or "")
        logger.info(
            "vlm_line_classify", readings=len(result["line_readings"]),
            symbol=result["symbol"]["kind"] if result["symbol"] else None,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("vlm_line_classify_failed", error=str(exc)[:200])
        return {"line_readings": [], "symbol": None}


def crop_bytes_for_bbox(image_bytes: bytes, x0: float, y0: float, x1: float, y1: float,
                         padding_px: int = 20, highlight: bool = True) -> bytes | None:
    """Like ``crop_bytes_for_region`` but takes raw coordinates (entities
    don't carry a SourceRegion) and optionally draws a marker box around the
    highlighted area so the VLM knows which of possibly several lines in the
    crop is the one being asked about."""
    import io

    from PIL import Image, ImageDraw

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        cx0 = max(0, int(x0) - padding_px)
        cy0 = max(0, int(y0) - padding_px)
        cx1 = min(w, int(x1) + padding_px)
        cy1 = min(h, int(y1) + padding_px)
        if cx1 <= cx0 or cy1 <= cy0:
            return None
        crop = img.crop((cx0, cy0, cx1, cy1)).convert("RGB")
        if highlight:
            draw = ImageDraw.Draw(crop)
            draw.rectangle(
                [int(x0) - cx0, int(y0) - cy0, int(x1) - cx0, int(y1) - cy0],
                outline=(255, 0, 0), width=2,
            )
        scale = max(1, 220 // max(crop.width, crop.height, 1))
        if scale > 1:
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("vlm_line_crop_failed", error=str(exc)[:160])
        return None


def crop_bytes_for_region(image_bytes: bytes, region, padding_px: int = 12) -> bytes | None:
    """PNG-encode a padded crop of ``image_bytes`` around ``region``
    (SourceRegion-like: x0,y0,x1,y1). Returns None on any failure."""
    import io

    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        x0 = max(0, int(region.x0) - padding_px)
        y0 = max(0, int(region.y0) - padding_px)
        x1 = min(w, int(region.x1) + padding_px)
        y1 = min(h, int(region.y1) + padding_px)
        if x1 <= x0 or y1 <= y0:
            return None
        crop = img.crop((x0, y0, x1, y1))
        # Small dimension text upsamples poorly for OCR/VLM alike — same
        # rationale as text_preserve.py's _OCR_UPSCALE.
        scale = max(1, 200 // max(crop.width, 1))
        if scale > 1:
            crop = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("vlm_crop_extract_failed", error=str(exc)[:160])
        return None
