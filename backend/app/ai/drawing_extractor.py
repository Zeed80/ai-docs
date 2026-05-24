"""AI extraction pipeline for technical drawings.

Extracts manufacturing features (holes, pockets, surfaces, etc.) with
dimensions, tolerances, surface roughness, and GD&T from DXF or rasterized drawings.

All production drawings are confidential by default — processed locally only.
Cloud VLMs (Claude, Gemini) require allow_cloud=True and drawing.is_confidential=False.
"""

from __future__ import annotations

import base64
import json
import re
import structlog
from typing import Any, TYPE_CHECKING

from app.config import settings

if TYPE_CHECKING:
    from app.ai.router import AIRouter
    from app.db.models import Drawing

logger = structlog.get_logger()

# ── System prompts ─────────────────────────────────────────────────────────────

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
      "feature_type": "hole|pocket|surface|boss|groove|thread|chamfer|radius|slot|contour|weld|knurl|key_slot|spline|center_bore|other",
      "name": "Отверстие Ø10 H7",
      "description": "Сквозное отверстие с посадкой H7",
      "confidence": 0.95,
      "source_view": "front",
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
- feature_type выбирай точно: hole (отверстие), pocket (карман), surface (плоскость/поверхность), boss (бобышка), groove (канавка/проточка), thread (резьба), chamfer (фаска), radius (радиус скругления), slot (шпоночный паз/паз), contour (внешний контур), weld (сварной шов), knurl (накатка), key_slot (шпоночный паз), spline (шлицы), center_bore (центровое отверстие)
- Для отверстий указывай dim_type: "diameter", для линейных размеров — "linear"
- Допуска: upper_tol — верхнее отклонение (положительное для H7), lower_tol — нижнее (обычно 0 для H)
- fit_system — квалитет (H7, h6, js5, k6, и т.п.)
- Шероховатость Ra указывается в мкм (Ra 1.6 = value: 1.6)
- Если шероховатость не указана явно — Ra 12.5 (обработано) или Ra 6.3 (чисто обработано)
- Если параметр неизвестен — null, не пропускай поле
- params для circle: {cx, cy, r} — координаты центра и радиус в мм
- params для rectangle: {x, y, width, height, rotation} — левый нижний угол, размеры, поворот в градусах
- params для polyline: {points: [[x1,y1],[x2,y2],...], closed: true/false}
- params для line: {x1, y1, x2, y2}
- params для arc: {cx, cy, r, start_angle, end_angle} — углы в градусах
- source_view: "front", "side", "top", "section_A-A", "isometric", "detail" и т.п.
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

# Специализированный промпт для чертежей деталей — упор на полноту допусков и шероховатостей
DETAIL_DRAWING_PROMPT = """Это чертёж детали (не сборочный).

Особые требования для чертежей деталей:
1. РАЗМЕРЫ И ДОПУСКА: извлеки ВСЕ проставленные размеры, включая вспомогательные и справочные (помечены знаком *)
2. ШЕРОХОВАТОСТЬ: укажи Ra/Rz для КАЖДОЙ поверхности. Если явно не указана — используй Ra 12.5 (по умолчанию обработанная поверхность) или значение общей шероховатости из углового штампа
3. GD&T: извлеки ВСЕ символы геометрических допусков с полными рамками, включая datum reference (A, B, C), material condition (M, L, R)
4. РЕЗЬБЫ: указывай полное обозначение (M12×1.5-6H, Tr40×6, G1/2"), класс точности, ход резьбы
5. ВНЕШНИЙ КОНТУР: обязательно включай feature_type: "contour" с основными габаритными размерами
6. Если видишь данатум (треугольник с буквой) — добавь его в gdt.datum_reference соответствующего элемента

Верни полный JSON со всеми элементами чертежа."""

# Специализированный промпт для сборочных чертежей
ASSEMBLY_DRAWING_PROMPT = """Это сборочный чертёж (не чертёж отдельной детали).

Особые требования для сборочных чертежей:
1. ПОСАДКИ: для каждого сопряжения указывай посадку в формате H7/k6 (отверстие/вал)
2. КРИТИЧЕСКИЕ ЗАЗОРЫ: извлеки зазоры и натяги в сопряжениях
3. БЕЗ ДЕТАЛЬНОЙ ГЕОМЕТРИИ: не пытайся извлекать геометрию отдельных деталей
4. СБОРОЧНЫЕ РАЗМЕРЫ: габаритные и присоединительные размеры всего изделия
5. ПОРЯДОК СБОРКИ: если есть указания на последовательность сборки — включи в description

В поле features включи:
- Сопряжения с посадками как feature_type: "surface" с fit_system
- Габаритные размеры как feature_type: "contour"
- Присоединительные размеры как feature_type: "surface"

Верни полный JSON. Не перечисляй внутренние детали — только интерфейсы между ними."""

# Специализированный промпт для сечений и разрезов
SECTION_VIEW_PROMPT = """Это вид сечения или разреза технического чертежа.

Особые требования для сечений:
1. ВНУТРЕННИЕ ЭЛЕМЕНТЫ: сосредоточься на внутренней геометрии, невидимой на других видах
2. ТОЛЩИНА СТЕНОК: измерь расстояние между внутренней и внешней поверхностью
3. ГЛУБИНЫ: для каждого кармана, глухого отверстия — укажи dim_type: "depth"
4. ШТРИХОВКА: интерпретируй паттерн штриховки по ГОСТ 3.1128:
   - косая (45°) — металл (сталь, чугун, алюминий)
   - перекрёстная — дерево, пластмасса
   - точечная — резина, уплотнения
5. ВНУТРЕННИЕ РЕЗЬБЫ: особенно важны — видны только в разрезе

Помечай feature_type для внутренних элементов, добавляй source_view: "section_X-X" с меткой сечения.

Верни полный JSON с акцентом на внутреннюю геометрию."""

# Специализированный промпт для сварочных чертежей
WELD_DRAWING_PROMPT = """Это сварочный чертёж или чертёж сварной конструкции.

Особые требования по ГОСТ 2.312:
1. ТИП ШВА: стыковой (С), угловой (У), нахлёсточный (Н), тавровый (Т), торцевой, прорезной
2. РАЗМЕРЫ ШВА:
   - для углового шва: катет k (в мм)
   - для стыкового шва: ширина e, высота g
   - для прерывистого шва: длина l и шаг t (l/t)
3. КЛАСС КАЧЕСТВА: по ГОСТ 5264 (ручная дуговая), ГОСТ 14771 (в защитном газе), ГОСТ 8713 (под флюсом)
4. СВАРОЧНЫЙ МАТЕРИАЛ: марка электрода или проволоки (например: Э46, Св-08Г2С)
5. ПОЗИЦИЯ СВАРКИ: нижняя, вертикальная, горизонтальная, потолочная
6. ПОЛЕВОЙ ШОВ: отмечается флажком на стрелке-выноске

Каждый сварной шов — отдельный feature_type: "weld".

Верни полный JSON со всеми сварными швами."""

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

_PROMPT_MAP: dict[str, str] = {
    "detail": DETAIL_DRAWING_PROMPT,
    "assembly": ASSEMBLY_DRAWING_PROMPT,
    "section": SECTION_VIEW_PROMPT,
    "weld": WELD_DRAWING_PROMPT,
}


# ── Core extraction ────────────────────────────────────────────────────────────


async def extract_features_from_image(
    image_bytes: bytes | list[bytes],
    *,
    router: "AIRouter | None" = None,
    drawing: "Drawing | None" = None,
    model: str | None = None,
    hint_text: str | None = None,
    drawing_type: str = "detail",
    view_labels: list[str] | None = None,
    allow_cloud: bool = False,
    few_shot_examples: list[dict] | None = None,
) -> dict[str, Any]:
    """Extract drawing features using a VLM from raw image bytes.

    Supports single image or list of images (multi-view analysis).
    Routes through AIRouter when provided (enforces confidentiality policy).
    Falls back to direct Ollama call when router is None (legacy path).

    Args:
        image_bytes: Single PNG bytes or list of PNG bytes for multi-view
        router: AIRouter instance for policy-aware dispatch (preferred)
        drawing: Drawing ORM instance (for confidential flag)
        model: VLM model name (used only when router is None)
        hint_text: Optional text context from OCR or DXF entities
        drawing_type: "detail"|"assembly"|"section"|"weld" — selects specialized prompt
        view_labels: Labels for each view image ["front", "side", "A-A"]
        allow_cloud: Allow cloud VLMs (only if drawing.is_confidential=False)
    """
    images = [image_bytes] if isinstance(image_bytes, bytes) else image_bytes
    labels = view_labels or [f"view_{i+1}" for i in range(len(images))]

    specialized_prompt = _PROMPT_MAP.get(drawing_type, DETAIL_DRAWING_PROMPT)
    context_part = ""
    if hint_text:
        context_part = f"\n\nДополнительный текстовый контекст с чертежа:\n{hint_text[:4000]}"

    few_shot_part = ""
    if few_shot_examples:
        lines = "\n".join(
            f"- {ex['description']} → \"{ex['correct_type']}\""
            for ex in few_shot_examples[:10]
        )
        few_shot_part = f"\n\n## Уточнения пользователей (приоритетные):\n{lines}"

    if len(images) > 1:
        view_list = "\n".join(f"- Изображение {i+1}: {labels[i]}" for i in range(len(images)))
        user_message = f"""Ты анализируешь многовидовой технический чертёж. Тебе предоставлены {len(images)} изображений:
{view_list}

{specialized_prompt}
{context_part}{few_shot_part}

Для каждого элемента укажи source_view из какого вида он извлечён.
Верни полный JSON без markdown-блоков."""
    else:
        user_message = f"""Ты анализируешь технический чертёж машиностроительной детали.

{specialized_prompt}
{context_part}{few_shot_part}

Верни полный JSON без markdown-блоков."""

    if router is not None:
        return await _extract_via_router(
            images=images,
            router=router,
            drawing=drawing,
            user_message=user_message,
            allow_cloud=allow_cloud,
        )

    # Legacy path — direct Ollama call
    return await _extract_legacy(
        images=images,
        model=model,
        user_message=user_message,
    )


async def _extract_via_router(
    images: list[bytes],
    router: "AIRouter",
    drawing: "Drawing | None",
    user_message: str,
    allow_cloud: bool,
) -> dict[str, Any]:
    """Dispatch VLM extraction through AIRouter with policy enforcement."""
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    confidential = True
    if drawing is not None and hasattr(drawing, "is_confidential"):
        confidential = drawing.is_confidential

    images_b64 = [base64.b64encode(img).decode() for img in images]

    # Check if resolved model supports multi-image; if not, process sequentially
    route = router.registry.get_route(AITask.DRAWING_ANALYSIS_VLM)
    resolved_model_name = _resolve_available_model(router, route.fallback_chain)
    supports_multi = False
    if resolved_model_name:
        cap = router.registry.get_model(resolved_model_name)
        supports_multi = getattr(cap, "supports_multi_image", False)

    if len(images) > 1 and not supports_multi:
        return await _extract_sequential_and_merge(
            images=images,
            router=router,
            drawing=drawing,
            user_message=user_message,
            allow_cloud=allow_cloud,
        )

    request = AIRequest(
        task=AITask.DRAWING_ANALYSIS_VLM,
        messages=[ChatMessage(role="user", content=user_message)],
        images=images_b64,
        confidential=confidential,
        allow_cloud=allow_cloud,
    )

    try:
        response = await router.run(request)
        raw_text = response.text or ""
        result = _parse_json_response(raw_text)
        if not isinstance(result, dict):
            logger.warning("vlm_router_invalid_json", raw=raw_text[:300])
            return {"title_block": {}, "features": []}
        return {
            "title_block": result.get("title_block") or {},
            "features": result.get("features") or [],
        }
    except Exception as exc:
        logger.error("vlm_router_extraction_failed", error=str(exc))
        return {"title_block": {}, "features": []}


async def _extract_sequential_and_merge(
    images: list[bytes],
    router: "AIRouter",
    drawing: "Drawing | None",
    user_message: str,
    allow_cloud: bool,
) -> dict[str, Any]:
    """Process views sequentially (for models without multi-image support) and merge."""
    results = []
    for idx, img in enumerate(images):
        result = await _extract_via_router(
            images=[img],
            router=router,
            drawing=drawing,
            user_message=user_message,
            allow_cloud=allow_cloud,
        )
        results.append(result)

    if len(results) == 1:
        return results[0]

    return _merge_multiview_results(results)


def _resolve_available_model(router: "AIRouter", fallback_chain: list[str]) -> str | None:
    """Return first model name in chain that exists in the registry."""
    for name in fallback_chain:
        try:
            router.registry.get_model(name)
            return name
        except KeyError:
            continue
    return None


async def _extract_legacy(
    images: list[bytes],
    model: str | None,
    user_message: str,
) -> dict[str, Any]:
    """Legacy path: direct Ollama call without router policy enforcement."""
    from app.ai.ollama_client import chat_with_images as ollama_vlm

    effective_model = model or getattr(
        settings, "ollama_model_vlm", getattr(settings, "ollama_model_ocr", "gemma4:e4b")
    )

    try:
        response = await ollama_vlm(
            prompt=user_message,
            images=images,
            model=effective_model,
            system=DRAWING_ANALYSIS_SYSTEM_PROMPT,
            temperature=0.1,
            max_tokens=8192,
            format_json=True,
        )
        raw_text = response.text if hasattr(response, "text") else str(response)
        result = _parse_json_response(raw_text)
        if not isinstance(result, dict):
            logger.warning("vlm_legacy_invalid_json", model=effective_model, raw=raw_text[:300])
            return {"title_block": {}, "features": []}
        return {
            "title_block": result.get("title_block") or {},
            "features": result.get("features") or [],
        }
    except Exception as exc:
        logger.error("vlm_legacy_extraction_failed", model=effective_model, error=str(exc))
        return {"title_block": {}, "features": []}


def _merge_multiview_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge features from multiple views with deduplication and confidence cross-validation."""
    merged_title = {}
    for r in results:
        tb = r.get("title_block") or {}
        if tb and not merged_title:
            merged_title = tb
        elif tb:
            # Fill missing fields from subsequent views
            for k, v in tb.items():
                if not merged_title.get(k) and v:
                    merged_title[k] = v

    all_features: list[dict] = []
    for r in results:
        all_features.extend(r.get("features") or [])

    # Deduplicate: same feature_type + nominal dimension ±2% = same feature
    deduped: list[dict] = []
    used_indices: set[int] = set()

    for i, feat_a in enumerate(all_features):
        if i in used_indices:
            continue
        group = [feat_a]
        group_indices = [i]
        nom_a = _primary_nominal(feat_a)
        ftype_a = feat_a.get("feature_type", "other")

        for j, feat_b in enumerate(all_features):
            if j <= i or j in used_indices:
                continue
            if feat_b.get("feature_type") != ftype_a:
                continue
            nom_b = _primary_nominal(feat_b)
            if nom_a and nom_b and abs(nom_a - nom_b) / max(nom_a, nom_b, 1e-9) < 0.02:
                group.append(feat_b)
                group_indices.append(j)

        for idx in group_indices:
            used_indices.add(idx)

        # Keep the highest-confidence version, merge sources
        best = max(group, key=lambda f: f.get("confidence", 0.0))
        views_seen = list({f.get("source_view") for f in group if f.get("source_view")})
        vote_count = len(group)

        best = dict(best)
        best["confirmed_by_views"] = views_seen
        best["confidence_votes"] = vote_count

        # Cross-validation: boost confidence if feature seen in multiple views
        conf = float(best.get("confidence", 0.5))
        if vote_count >= 2:
            conf = min(1.0, conf + 0.10)
        best["confidence"] = round(conf, 3)

        deduped.append(best)

    return {"title_block": merged_title, "features": deduped}


def _primary_nominal(feature: dict) -> float | None:
    """Return the primary (first) nominal dimension value of a feature."""
    for dim in feature.get("dimensions") or []:
        n = dim.get("nominal")
        if n is not None:
            try:
                return float(n)
            except (TypeError, ValueError):
                pass
    return None


async def _classify_drawing_type(
    title_block_text: str | None,
    router: "AIRouter | None" = None,
) -> str:
    """Classify drawing type from title block text.

    Returns: "detail" | "assembly" | "section" | "weld"
    """
    if not title_block_text:
        return "detail"

    text_lower = title_block_text.lower()

    # Heuristic: check for assembly keywords
    assembly_keywords = ["сборочный", "сб", " sb", "сборка", "узел", "блок", "агрегат"]
    weld_keywords = ["сварной", "сварка", "св.", "конструкция сварная"]
    if any(kw in text_lower for kw in assembly_keywords):
        return "assembly"
    if any(kw in text_lower for kw in weld_keywords):
        return "weld"

    # If a router is available, do a quick text classification
    if router is not None:
        try:
            from app.ai.schemas import AIRequest, AITask, ChatMessage
            classify_prompt = (
                "Определи тип технического чертежа по тексту штампа. "
                "Отвечай ТОЛЬКО одним словом: detail (чертёж детали), assembly (сборочный чертёж), "
                "section (вид сечения), weld (сварочный чертёж).\n\n"
                f"Текст штампа:\n{title_block_text[:500]}"
            )
            request = AIRequest(
                task=AITask.CLASSIFICATION,
                messages=[ChatMessage(role="user", content=classify_prompt)],
                confidential=True,
            )
            response = await router.run(request)
            text = (response.text or "").strip().lower().split()[0] if response.text else ""
            if text in ("detail", "assembly", "section", "weld"):
                return text
        except Exception:
            pass

    return "detail"


# ── Text-based extraction (fallback when no image available) ───────────────────


async def extract_drawing_features(
    drawing_text: str,
    drawing_entities: list[dict] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Extract features from drawing text/entities using AI (text-only fallback).

    Args:
        drawing_text: Text from PDF OCR or DXF text entities
        drawing_entities: Structured entities from DXF (optional)
        model: Model name (defaults from settings)
    """
    from app.ai.ollama_client import chat as ollama_chat

    effective_model = model or getattr(
        settings, "ollama_model_vlm", getattr(settings, "ollama_model_ocr", "gemma4:e4b")
    )

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
    """AI tool recommendation for a drawing feature.

    Returns: list of {entry_id, score, reason}
    """
    from app.ai.ollama_client import chat as ollama_chat

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
            messages=[{"role": "user", "content": prompt}],
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


# ── Helpers ────────────────────────────────────────────────────────────────────


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
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    text = text.strip()

    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        if start != -1:
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
    """Heuristically infer likely tool types for a feature type."""
    mapping: dict[str, list[str]] = {
        "hole": ["drill", "reamer", "boring_bar", "countersink", "counterbore"],
        "thread": ["tap", "thread_mill"],
        "pocket": ["endmill", "milling_cutter"],
        "slot": ["endmill", "milling_cutter"],
        "key_slot": ["endmill", "milling_cutter"],
        "groove": ["turning_tool", "endmill"],
        "surface": ["endmill", "milling_cutter", "turning_tool", "grinder"],
        "boss": ["turning_tool", "endmill"],
        "chamfer": ["countersink", "endmill", "turning_tool"],
        "radius": ["endmill", "turning_tool"],
        "contour": ["endmill", "turning_tool"],
        "spline": ["broach", "grinding_wheel", "endmill"],
        "knurl": ["knurling_tool", "turning_tool"],
        "center_bore": ["center_drill", "drill"],
    }
    return mapping.get(feature_type, ["endmill", "drill", "turning_tool"])
