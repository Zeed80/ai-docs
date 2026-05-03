"""AI extraction pipeline for technical drawings.

Extracts manufacturing features (holes, pockets, surfaces, etc.) with
dimensions, tolerances, surface roughness, and GD&T from DXF or rasterized drawings.
Uses gemma4:e4b (local Ollama) for confidential processing.
"""

import json
import re
import structlog
from typing import Any

from app.ai.ollama_client import chat as ollama_chat, chat_with_images as ollama_vlm
from app.config import settings

logger = structlog.get_logger()

DRAWING_ANALYSIS_SYSTEM_PROMPT = """Ты — специализированная ИИ-система для анализа технических чертежей машиностроительных деталей.
Твоя задача — точно извлечь все конструктивные элементы чертежа и их параметры.

Анализируй и возвращай СТРОГО JSON без markdown-блоков. Структура:
{
  "title_block": {
    "title": "Вал ведомый",
    "drawing_number": "ДП-001",
    "revision": "А",
    "developer": "Иванов И.И.",
    "checker": null,
    "approver": null,
    "date": "2024-01",
    "scale": "1:1",
    "material": "Сталь 45 ГОСТ 1050",
    "mass_kg": 2.5,
    "sheet": 1,
    "sheets_total": 1
  },
  "features": [
    {
      "feature_type": "hole|pocket|surface|boss|groove|thread|chamfer|radius|slot|contour|other",
      "name": "Отверстие Ø10 H7",
      "description": "Сквозное отверстие с посадкой H7",
      "confidence": 0.95,
      "contours": [
        {
          "primitive_type": "circle|arc|rectangle|polyline|line",
          "params": {"cx": 50.0, "cy": 30.0, "r": 5.0},
          "layer": "0",
          "line_type": "solid|dashed|dotted|center|phantom"
        }
      ],
      "dimensions": [
        {
          "dim_type": "diameter|linear|angular|radius|depth",
          "nominal": 10.0,
          "upper_tol": 0.015,
          "lower_tol": 0.0,
          "unit": "mm",
          "fit_system": "H7",
          "label": "Ø10 H7"
        }
      ],
      "surfaces": [
        {
          "roughness_type": "Ra|Rz|Rmax",
          "value": 1.6,
          "machining_required": true
        }
      ],
      "gdt": [
        {
          "symbol": "cylindricity|perpendicularity|flatness|position|etc",
          "tolerance_value": 0.01,
          "datum_reference": "A"
        }
      ]
    }
  ]
}

Правила:
- feature_type выбирай точно: hole (отверстие), pocket (карман), surface (плоскость/поверхность), boss (бобышка), groove (канавка/проточка), thread (резьба), chamfer (фаска), radius (радиус скругления), slot (шпоночный паз/паз), contour (внешний контур детали)
- Для отверстий указывай dim_type: "diameter", для линейных размеров — "linear"
- Допуска: upper_tol — верхнее отклонение (положительное для H7), lower_tol — нижнее (обычно 0 для H)
- fit_system — квалитет (H7, h6, js5, k6, и т.п.)
- Шероховатость Ra указывается в мкм (Ra 1.6 = value: 1.6)
- Если параметр неизвестен — null, не пропускай поле
- params для circle: {cx, cy, r} — координаты центра и радиус в мм
- params для rectangle: {x, y, width, height, rotation} — левый нижний угол, размеры, поворот в градусах
- params для polyline: {points: [[x1,y1],[x2,y2],...], closed: true/false}
- params для line: {x1, y1, x2, y2}
- params для arc: {cx, cy, r, start_angle, end_angle} — углы в градусах
"""

TITLE_BLOCK_EXTRACTION_PROMPT = """Извлеки штамп (основную надпись) чертежа из предоставленного текста/описания.
Верни СТРОГО JSON:
{
  "title": "наименование детали",
  "drawing_number": "обозначение",
  "revision": "литера",
  "developer": "разработчик",
  "checker": "проверил",
  "approver": "утвердил",
  "date": "дата",
  "scale": "масштаб",
  "material": "материал",
  "mass_kg": null,
  "sheet": 1,
  "sheets_total": 1
}
"""

TOOL_SUGGESTION_PROMPT = """Ты — технолог-эксперт по металлообработке. Подбери режущий инструмент для конструктивного элемента.

Элемент: {feature_description}
Тип: {feature_type}
Основной размер: {main_dimension}
Шероховатость: {roughness}
Материал детали: {material}

Доступные инструменты из базы данных:
{available_tools}

Выбери наиболее подходящие инструменты и объясни выбор. Верни СТРОГО JSON:
[
  {
    "entry_id": "uuid инструмента из базы",
    "score": 0.95,
    "reason": "Обоснование выбора: диаметр соответствует, материал HSS подходит для стали 45"
  }
]
"""


async def extract_features_from_image(
    image_bytes: bytes,
    *,
    model: str | None = None,
    hint_text: str | None = None,
) -> dict[str, Any]:
    """Extract drawing features using a VLM (Vision Language Model) from raw image bytes.

    This is the primary extraction path for raster images (PNG, JPG, TIFF, etc.)
    and also serves as a complementary pass for DXF/PDF after rasterization.

    Args:
        image_bytes: Raw image bytes (PNG preferred for best quality)
        model: VLM model name (defaults to settings.ollama_model_vlm)
        hint_text: Optional text context (from OCR or DXF entities) to guide the model

    Returns:
        Dict with title_block and features
    """
    effective_model = model or getattr(settings, "ollama_model_vlm", getattr(settings, "ollama_model_ocr", "gemma4:e4b"))

    context_part = ""
    if hint_text:
        context_part = f"\n\nДополнительный текстовый контекст с чертежа:\n{hint_text[:4000]}"

    user_message = f"""Ты анализируешь технический чертёж машиностроительной детали.

Внимательно рассмотри изображение и извлеки:
1. Штамп (основную надпись): название детали, обозначение, материал, масштаб, масса
2. Все конструктивные элементы: отверстия, карманы, поверхности, резьбы, фаски, радиусы, пазы
3. Для каждого элемента: размеры с допусками, шероховатость (Ra/Rz), GD&T символы
4. Координаты контуров элементов на чертеже (в мм, если масштаб известен)
{context_part}

Верни полный JSON без markdown-блоков."""

    try:
        response = await ollama_vlm(
            prompt=user_message,
            images=[image_bytes],
            model=effective_model,
            system=DRAWING_ANALYSIS_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=8192,
            format_json=True,
        )

        raw_text = response.text if hasattr(response, "text") else str(response)
        result = _parse_json_response(raw_text)

        if not isinstance(result, dict):
            logger.warning("vlm_extraction_invalid_json", model=effective_model, raw=raw_text[:300])
            return {"title_block": {}, "features": []}

        return {
            "title_block": result.get("title_block") or {},
            "features": result.get("features") or [],
        }

    except Exception as exc:
        logger.error("vlm_extraction_failed", model=effective_model, error=str(exc))
        return {"title_block": {}, "features": []}


async def extract_drawing_features(
    drawing_text: str,
    drawing_entities: list[dict] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Extract features from drawing text/entities using AI.
    
    Args:
        drawing_text: Текстовое описание чертежа (из PDF OCR или DXF текстовых объектов)
        drawing_entities: Структурированные сущности из DXF (опционально)
        model: Название модели (по умолчанию из настроек)
    
    Returns:
        Dict с title_block и features
    """
    effective_model = model or getattr(settings, "ollama_model_vlm", getattr(settings, "ollama_model_ocr", "gemma4:e4b"))

    entities_context = ""
    if drawing_entities:
        entities_summary = _summarize_dxf_entities(drawing_entities)
        entities_context = f"\n\nСтруктурированные объекты DXF:\n{entities_summary}"

    user_message = f"""Проанализируй технический чертёж и извлеки все конструктивные элементы.

Текст с чертежа:
{drawing_text[:8000]}
{entities_context}

Верни полный JSON со всеми элементами чертежа."""

    try:
        response = await ollama_chat(
            model=effective_model,
            messages=[
                {"role": "system", "content": DRAWING_ANALYSIS_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
        )

        raw_text = response.text if hasattr(response, "text") else (
            response.get("message", {}).get("content", "") if isinstance(response, dict) else str(response)
        )
        result = _parse_json_response(raw_text)

        if not isinstance(result, dict):
            logger.warning("drawing_extraction_invalid_json", raw=raw_text[:200])
            return {"title_block": {}, "features": []}

        return {
            "title_block": result.get("title_block") or {},
            "features": result.get("features") or [],
        }

    except Exception as exc:
        logger.error("drawing_extraction_failed", error=str(exc))
        return {"title_block": {}, "features": []}


async def suggest_tools_for_feature(
    feature: dict[str, Any],
    available_tools: list[dict[str, Any]],
    material: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """
    AI-подбор инструментов для конструктивного элемента.
    
    Returns: list of {entry_id, score, reason}
    """
    effective_model = model or getattr(settings, "drawing_ai_model", "gemma3:4b")

    feature_type = feature.get("feature_type", "other")
    feature_name = feature.get("name", "")
    description = feature.get("description", "")
    feature_description = f"{feature_name}. {description}".strip()

    main_dimension = ""
    for dim in feature.get("dimensions", []):
        if dim.get("dim_type") in ("diameter", "linear"):
            nominal = dim.get("nominal", 0)
            fit = dim.get("fit_system", "")
            label = dim.get("label", "")
            main_dimension = label or f"{nominal} {fit}".strip()
            break

    roughness = ""
    for surf in feature.get("surfaces", []):
        r_type = surf.get("roughness_type", "Ra")
        r_val = surf.get("value", "")
        roughness = f"{r_type} {r_val} мкм"
        break

    tools_text = json.dumps(available_tools[:10], ensure_ascii=False, indent=2)

    prompt = TOOL_SUGGESTION_PROMPT.format(
        feature_description=feature_description,
        feature_type=feature_type,
        main_dimension=main_dimension or "не указан",
        roughness=roughness or "не указана",
        material=material or "не указан",
        available_tools=tools_text,
    )

    try:
        response = await ollama_chat(
            model=effective_model,
            messages=[
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )

        raw_text = response.get("message", {}).get("content", "") if isinstance(response, dict) else str(response)
        result = _parse_json_response(raw_text)

        if isinstance(result, list):
            return result[:5]
        return []

    except Exception as exc:
        logger.error("tool_suggestion_failed", error=str(exc))
        return []


def _summarize_dxf_entities(entities: list[dict]) -> str:
    """Create a concise summary of DXF entities for AI context."""
    circles = [e for e in entities if e.get("type") == "CIRCLE"]
    lines = [e for e in entities if e.get("type") == "LINE"]
    arcs = [e for e in entities if e.get("type") == "ARC"]
    texts = [e for e in entities if e.get("type") in ("TEXT", "MTEXT")]
    dims = [e for e in entities if e.get("type") == "DIMENSION"]

    summary_parts = []
    if circles:
        circle_descs = [
            f"  R={e.get('radius', 0):.2f} center=({e.get('center_x', 0):.1f},{e.get('center_y', 0):.1f})"
            for e in circles[:20]
        ]
        summary_parts.append(f"Окружности ({len(circles)}):\n" + "\n".join(circle_descs))

    if arcs:
        arc_descs = [
            f"  R={e.get('radius', 0):.2f} {e.get('start_angle', 0):.0f}°-{e.get('end_angle', 0):.0f}°"
            for e in arcs[:10]
        ]
        summary_parts.append(f"Дуги ({len(arcs)}):\n" + "\n".join(arc_descs))

    if lines:
        summary_parts.append(f"Линий: {len(lines)}")

    if texts:
        text_values = [e.get("text", "") for e in texts[:30] if e.get("text")]
        summary_parts.append("Тексты:\n" + "\n".join(f"  {t}" for t in text_values))

    if dims:
        dim_descs = [
            f"  {e.get('measurement', 0):.3f} {e.get('dim_type', '')}"
            for e in dims[:20]
        ]
        summary_parts.append(f"Размеры ({len(dims)}):\n" + "\n".join(dim_descs))

    return "\n\n".join(summary_parts) or "Нет данных"


def _parse_json_response(text: str) -> Any:
    """Parse JSON from AI response, stripping markdown code blocks."""
    text = text.strip()
    # Remove markdown code blocks
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = text.strip()

    # Find first JSON object or array
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start != -1:
            # Find matching end
            depth = 0
            in_string = False
            escape_next = False
            for i, ch in enumerate(text[start:], start):
                if escape_next:
                    escape_next = False
                    continue
                if ch == "\\" and in_string:
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                if not in_string:
                    if ch == start_char:
                        depth += 1
                    elif ch == end_char:
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(text[start : i + 1])
                            except json.JSONDecodeError:
                                break

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def infer_tool_type_for_feature(feature_type: str, dimensions: list[dict]) -> list[str]:
    """
    Heuristically infer likely tool types for a feature type.
    Used to filter tool catalog before AI suggestion.
    """
    mapping: dict[str, list[str]] = {
        "hole": ["drill", "reamer", "boring_bar", "countersink", "counterbore"],
        "thread": ["tap", "thread_mill"],
        "pocket": ["endmill", "milling_cutter"],
        "slot": ["endmill", "milling_cutter"],
        "groove": ["turning_tool", "endmill"],
        "surface": ["endmill", "milling_cutter", "turning_tool", "grinder"],
        "boss": ["turning_tool", "endmill"],
        "chamfer": ["countersink", "endmill", "turning_tool"],
        "radius": ["endmill", "turning_tool"],
        "contour": ["endmill", "turning_tool"],
    }
    return mapping.get(feature_type, ["endmill", "drill", "turning_tool"])
