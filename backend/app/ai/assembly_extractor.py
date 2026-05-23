"""Assembly drawing BOM and balloon extractor.

Extracts Bill of Materials (BoM) from assembly drawings:
- OpenCV: table detection (high aspect-ratio contours in upper-right — GOST BOM position)
- VLM via AIRouter: structured BOM JSON
- cv2.HoughCircles: balloon (позиционные обозначения) detection

All operations are synchronous (run in Celery/executor context).
"""

from __future__ import annotations

import io
import re
import structlog
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.ai.router import AIRouter
    from app.db.models import Drawing

logger = structlog.get_logger()


@dataclass
class BOMItem:
    item_no: int
    designation: str
    quantity: float
    unit: str | None = None
    material: str | None = None
    drawing_number: str | None = None
    note: str | None = None
    balloon_coords: list[dict] | None = None   # [{"x": int, "y": int, "r": int}]
    confidence: float = 0.0


@dataclass
class BalloonAnnotation:
    item_no: int
    x: int
    y: int
    radius: int


@dataclass
class AssemblyBOMResult:
    items: list[BOMItem] = field(default_factory=list)
    balloons: list[BalloonAnnotation] = field(default_factory=list)
    table_bbox: tuple[int, int, int, int] | None = None   # (x, y, w, h)
    confidence: float = 0.0


# ── Public API ─────────────────────────────────────────────────────────────────


async def extract_assembly_bom(
    image_bytes: bytes,
    router: "AIRouter | None" = None,
    drawing: "Drawing | None" = None,
    allow_cloud: bool = False,
) -> AssemblyBOMResult:
    """Extract BOM from an assembly drawing image.

    Stage 1: OpenCV table detection — crop the BOM table region.
    Stage 2: VLM extraction — parse the table into structured JSON.
    Stage 3: Balloon detection — link item numbers to their positions.
    """
    result = AssemblyBOMResult()

    # Stage 1: locate table region
    table_crop_bytes, table_bbox = _detect_bom_table(image_bytes)
    result.table_bbox = table_bbox

    if table_crop_bytes is None:
        table_crop_bytes = image_bytes  # fall back to full image

    # Stage 2: VLM extraction
    bom_items = await _extract_bom_via_vlm(
        table_crop_bytes,
        router=router,
        drawing=drawing,
        allow_cloud=allow_cloud,
    )
    result.items = bom_items
    if bom_items:
        result.confidence = sum(i.confidence for i in bom_items) / len(bom_items)

    # Stage 3: balloon detection on full drawing
    result.balloons = _detect_balloons(image_bytes)

    # Link balloon coordinates to BOM items by item_no
    _link_balloons_to_items(result)

    logger.info(
        "assembly_bom_extracted",
        items=len(result.items),
        balloons=len(result.balloons),
        confidence=round(result.confidence, 3),
    )
    return result


# ── OpenCV: BOM table detection ────────────────────────────────────────────────


def _detect_bom_table(image_bytes: bytes) -> tuple[bytes | None, tuple[int, int, int, int] | None]:
    """Find the BOM table in the drawing (GOST: upper-right corner).

    Returns (cropped_table_bytes, (x, y, w, h)) or (None, None) on failure.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_np = np.array(img)
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape[:2]

        # Look in upper-right quadrant (GOST BOM is in upper-right)
        roi_x = w // 2
        roi_y = 0
        roi_w = w - roi_x
        roi_h = h // 2
        roi = gray[roi_y:roi_y + roi_h, roi_x:roi_x + roi_w]

        _, binary = cv2.threshold(roi, 180, 255, cv2.THRESH_BINARY_INV)

        # Find contours with high aspect ratio (table rows are wide + thin)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        table_candidates = []
        for cnt in contours:
            bx, by, bw, bh = cv2.boundingRect(cnt)
            aspect = bw / max(bh, 1)
            area = bw * bh
            # Table rows: wide (aspect > 4) and covering significant width
            if aspect > 4 and bw > roi_w * 0.4 and area > 500:
                table_candidates.append((bx, by, bw, bh))

        if not table_candidates:
            return None, None

        # Merge overlapping / nearby table rows into one table bbox
        table_candidates.sort(key=lambda r: r[1])  # sort by y
        merged = _merge_table_rects(table_candidates, gap_tolerance=20)

        if not merged:
            return None, None

        # Pick the largest merged region
        merged.sort(key=lambda r: r[2] * r[3], reverse=True)
        best = merged[0]
        tx, ty, tw, th = best

        # Convert back to full-image coordinates
        abs_x = roi_x + tx
        abs_y = roi_y + ty

        # Add margin
        margin = 10
        abs_x = max(0, abs_x - margin)
        abs_y = max(0, abs_y - margin)
        tw = min(w - abs_x, tw + 2 * margin)
        th = min(h - abs_y, th + 2 * margin)

        # Crop from full image
        crop = img.crop((abs_x, abs_y, abs_x + tw, abs_y + th))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        crop_bytes = buf.getvalue()

        return crop_bytes, (abs_x, abs_y, tw, th)

    except ImportError:
        logger.debug("assembly_extractor_no_cv2")
        return None, None
    except Exception as exc:
        logger.warning("bom_table_detection_failed", error=str(exc))
        return None, None


def _merge_table_rects(
    rects: list[tuple[int, int, int, int]],
    gap_tolerance: int = 20,
) -> list[tuple[int, int, int, int]]:
    """Merge vertically adjacent rectangles into groups."""
    if not rects:
        return []

    groups: list[list[tuple[int, int, int, int]]] = [[rects[0]]]
    for rect in rects[1:]:
        _, by, _, bh = rect
        last_group = groups[-1]
        last_y, last_h = last_group[-1][1], last_group[-1][3]
        if by <= last_y + last_h + gap_tolerance:
            last_group.append(rect)
        else:
            groups.append([rect])

    merged = []
    for group in groups:
        min_x = min(r[0] for r in group)
        min_y = min(r[1] for r in group)
        max_x = max(r[0] + r[2] for r in group)
        max_y = max(r[1] + r[3] for r in group)
        merged.append((min_x, min_y, max_x - min_x, max_y - min_y))
    return merged


# ── VLM: BOM parsing ──────────────────────────────────────────────────────────

_ASSEMBLY_BOM_PROMPT = """Ты анализируешь таблицу спецификации (ведомость) сборочного чертежа по ГОСТ 2.106.

Извлеки строки таблицы и верни СТРОГО JSON без markdown-блоков:
{
  "items": [
    {
      "item_no": 1,
      "designation": "Вал",
      "quantity": 1.0,
      "unit": "шт",
      "material": "Сталь 45 ГОСТ 1050",
      "drawing_number": "ДП-001",
      "note": null,
      "confidence": 0.95
    }
  ]
}

Правила:
- item_no — позиционный номер (целое число)
- designation — наименование позиции
- quantity — количество (число с дробью если нужно)
- unit — единица измерения (шт, кг, м, компл, и т.д.)
- material — материал (если указан в спецификации)
- drawing_number — номер чертежа позиции (если указан)
- note — примечание
- confidence 0.0-1.0

Если таблица содержит разделы (детали / стандартные изделия / материалы) — выведи все позиции.
Не включай заголовочные строки. Верни только JSON."""


async def _extract_bom_via_vlm(
    image_bytes: bytes,
    router: "AIRouter | None",
    drawing: "Drawing | None",
    allow_cloud: bool,
) -> list[BOMItem]:
    """Call VLM to parse BOM table image into structured items."""
    import base64

    if router is None:
        # Direct Ollama fallback
        from app.ai.ollama_client import chat_with_images  # type: ignore
        from app.config import settings

        b64 = base64.b64encode(image_bytes).decode()
        response_text = await chat_with_images(
            model=getattr(settings, "model_vlm", None) or "qwen3.6:35b",
            system_prompt=_ASSEMBLY_BOM_PROMPT,
            user_message="Извлеки спецификацию из таблицы на изображении.",
            images=[b64],
        )
    else:
        from app.ai.schemas import AITask, AIRequest

        is_confidential = getattr(drawing, "is_confidential", True) if drawing else True
        b64 = base64.b64encode(image_bytes).decode()
        request = AIRequest(
            task=AITask.DRAWING_ANALYSIS_VLM,
            messages=[
                {"role": "system", "content": _ASSEMBLY_BOM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": "Извлеки спецификацию из таблицы на изображении."},
                    ],
                },
            ],
            confidential=is_confidential,
            allow_cloud=allow_cloud,
        )
        response = await router.run(request)
        response_text = response.content if hasattr(response, "content") else str(response)

    return _parse_bom_json(response_text)


def _parse_bom_json(text: str) -> list[BOMItem]:
    """Parse VLM response into BOMItem list."""
    import json

    # Strip markdown fences
    text = re.sub(r"```(?:json)?", "", text).strip()

    # Find JSON object
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return []

    try:
        data = json.loads(match.group(0))
        raw_items = data.get("items", [])
        if not isinstance(raw_items, list):
            return []

        result = []
        for item in raw_items:
            try:
                result.append(BOMItem(
                    item_no=int(item.get("item_no", 0)),
                    designation=str(item.get("designation", ""))[:500],
                    quantity=float(item.get("quantity", 1)),
                    unit=item.get("unit"),
                    material=item.get("material"),
                    drawing_number=item.get("drawing_number"),
                    note=item.get("note"),
                    confidence=float(item.get("confidence", 0.7)),
                ))
            except (TypeError, ValueError):
                pass
        return result
    except Exception as exc:
        logger.warning("bom_json_parse_failed", error=str(exc))
        return []


# ── OpenCV: Balloon detection ─────────────────────────────────────────────────


def _detect_balloons(image_bytes: bytes) -> list[BalloonAnnotation]:
    """Detect positional balloons (circles with item numbers) in drawing.

    Uses cv2.HoughCircles for circle detection, then pytesseract OCR to
    read the number inside each detected circle.

    Balloon radius range: 15–50px at 200 DPI (8–25mm actual diameter).
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_np = np.array(img)
        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape[:2]

        # Scale radius range with image resolution
        scale = max(w, h) / 3000.0
        min_r = max(10, int(15 * scale))
        max_r = max(30, int(60 * scale))

        circles = cv2.HoughCircles(
            gray,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=min_r * 2,
            param1=60,
            param2=35,
            minRadius=min_r,
            maxRadius=max_r,
        )

        if circles is None:
            return []

        circles = np.round(circles[0, :]).astype(int)
        balloons: list[BalloonAnnotation] = []

        for cx, cy, cr in circles:
            item_no = _ocr_circle(img_np, cx, cy, cr)
            if item_no is not None and item_no > 0:
                balloons.append(BalloonAnnotation(
                    item_no=item_no,
                    x=int(cx),
                    y=int(cy),
                    radius=int(cr),
                ))

        logger.info("balloon_detection", found=len(circles), parsed=len(balloons))
        return balloons

    except ImportError:
        logger.debug("balloon_detection_no_cv2")
        return []
    except Exception as exc:
        logger.warning("balloon_detection_failed", error=str(exc))
        return []


def _ocr_circle(img_np: Any, cx: int, cy: int, cr: int) -> int | None:
    """OCR the number inside a circle region."""
    try:
        import pytesseract
        import cv2
        import numpy as np
        from PIL import Image

        # Crop with padding
        pad = max(4, int(cr * 0.2))
        x1 = max(0, cx - cr - pad)
        y1 = max(0, cy - cr - pad)
        x2 = min(img_np.shape[1], cx + cr + pad)
        y2 = min(img_np.shape[0], cy + cr + pad)
        crop = img_np[y1:y2, x1:x2]

        if crop.size == 0:
            return None

        # Upscale for better OCR
        scale = max(1.0, 80 / max(crop.shape[:2]))
        if scale > 1:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        # Binarize
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        pil_crop = Image.fromarray(binary)
        config = "--psm 10 -c tessedit_char_whitelist=0123456789"
        text = pytesseract.image_to_string(pil_crop, config=config).strip()
        text = re.sub(r"[^0-9]", "", text)
        return int(text) if text else None

    except ImportError:
        # pytesseract not installed — skip OCR
        return None
    except Exception:
        return None


# ── Linking ───────────────────────────────────────────────────────────────────


def _link_balloons_to_items(result: AssemblyBOMResult) -> None:
    """Attach balloon coordinates to matching BOM items."""
    balloon_map: dict[int, list[dict]] = {}
    for b in result.balloons:
        balloon_map.setdefault(b.item_no, []).append({"x": b.x, "y": b.y, "r": b.radius})

    for item in result.items:
        if item.item_no in balloon_map:
            item.balloon_coords = balloon_map[item.item_no]
