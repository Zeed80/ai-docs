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
