"""Спросить у модели, что она умеет, вместо того чтобы верить каталогу.

Каталог заполняется автоматически из ответа провайдера на ``/v1/models`` и
ошибается в обе стороны. На этом стенде обе ошибки встретились сразу:

* ``ollama_cloud deepseek-v3.1:671b`` объявлен БЕЗ зрения — и прочитал чертёж,
  вернув Ø15,7 / Ø24,5 / Ø21,7, которые независимо совпали с фрагментным
  чтением. Каталог занижал.
* ``openrouter minimax-m3:free`` объявлен пригодным кандидатом — и строгую
  схему не держит: три прохода полного чтения из пяти отвалились. Каталог
  завышал.

Пока возможности не проверены, гейт назначения не может быть строгим: запретить
модель по недостоверным метаданным значит запретить работающую. Поэтому
несоответствие модальности блокирует назначение только тогда, когда возможность
ПРОВЕРЕНА, и остаётся предупреждением, пока о ней просто не знают.

Методология скопирована с уже работающей `_ollama_probe_thinking_levels`:

* ``None`` при сетевом сбое или таймауте — и НИКОГДА ``False``. Инфраструктурная
  икота не должна закешироваться как приговор модели;
* ``False`` только при явном отказе или явно неверном содержательном ответе;
* детерминизм: температура 0, крошечная картинка, короткий лимит вывода;
* бюджет — три-четыре коротких вызова на модель, запуск по кнопке, не в опросе.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

logger = structlog.get_logger()

# Цифра, которую модель должна назвать, посмотрев на картинку. Выбрана так,
# чтобы её нельзя было угадать: «1» и «0» модель называет и вслепую.
_VISION_DIGIT = "7"
_VISION_PROMPT = "На картинке одна цифра. Назови её. Ответь ОДНИМ символом, без пояснений."
# Вторая цифра — для пробы нескольких изображений. Модель, читающая только
# первый кадр, назовёт «7» и промолчит про «4»; читающая оба — назовёт обе.
_SECOND_DIGIT = "4"
_MULTI_IMAGE_PROMPT = (
    "Тебе передано ДВА изображения, на каждом одна цифра. Назови обе по "
    "порядку через запятую. Только цифры."
)
_STRUCTURED_PROMPT = "Верни JSON: поле answer со значением 42, поле label со значением ok."
_STRUCTURED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer", "label"],
    "properties": {"answer": {"type": "integer"}, "label": {"type": "string"}},
}


def _answered_by_the_named_model(model_key: str, response: Any) -> bool:
    """Ответила ли именно та модель, о которой спрашивали.

    `preferred_model` ставит модель ПЕРВОЙ в цепочку, но цепочкой не
    ограничивает: если названная модель отказала, роутер молча берёт следующего
    кандидата. Для обычного вызова это правильно, для пробы — губительно.
    Поймано живьём: проба embedding-модели вернула «зрение есть», хотя в журнале
    стояло `ai_route_model_failed` с 400 на ней самой, а цифру назвал фолбэк.

    Приписать чужой ответ проверяемой модели — ровно тот молчаливый подлог,
    ради борьбы с которым проба и заводилась.
    """
    from app.ai.model_registry import ModelRegistry

    try:
        cap = ModelRegistry.from_yaml("backend/app/ai/config/model_registry.yaml").models.get(
            model_key
        )
    except Exception:  # noqa: BLE001 — без каталога сверить не с чем
        return True
    if cap is None:
        return True
    answered = str(getattr(response, "model", "") or "")
    return answered in ("", model_key, cap.provider_model)


@dataclass(frozen=True)
class ProbeResult:
    """Что удалось установить. ``None`` — «не определили»."""

    vision: bool | None = None
    structured: bool | None = None
    multi_image: bool | None = None
    checked_at: str = ""
    details: dict[str, Any] | None = None


def _digit_image(digit: str = _VISION_DIGIT) -> str:
    """64×64 PNG с крупной цифрой, base64 — без файлов и внешних ресурсов."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(image)
    # Рисуем отрезками: шрифты в контейнере могут отсутствовать, а проба
    # обязана работать везде, где работает сам сервис.
    if digit == "7":
        draw.line([(12, 12), (52, 12)], fill="black", width=6)
        draw.line([(52, 12), (26, 54)], fill="black", width=6)
    elif digit == "4":
        draw.line([(40, 10), (14, 40)], fill="black", width=6)
        draw.line([(14, 40), (54, 40)], fill="black", width=6)
        draw.line([(40, 10), (40, 56)], fill="black", width=6)
    else:  # pragma: no cover — в пробе используются только две цифры
        raise ValueError(digit)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


async def probe_vision(model_key: str, *, router: Any = None) -> tuple[bool | None, str]:
    """Видит ли модель картинку на самом деле."""
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.CAD_TEXT_OCR,
        messages=[ChatMessage(role="user", content=_VISION_PROMPT)],
        images=[_digit_image()],
        confidential=False,
        allow_cloud=True,
        preferred_model=model_key,
        thinking=False,
        max_output_tokens=16,
        inference_params={"temperature": 0},
        metadata={"format_max_reasks": 0},
    )
    try:
        response = await router.run(request)
    except Exception as exc:  # noqa: BLE001 — «не смогли спросить» ≠ «не умеет»
        logger.info("capability_probe_vision_unavailable", model=model_key, error=str(exc)[:200])
        return None, f"проба не выполнена: {type(exc).__name__}"
    if not _answered_by_the_named_model(model_key, response):
        return None, f"ответил другой кандидат цепочки ({response.model}) — проба недействительна"
    answer = (response.text or "").strip()
    if not answer:
        # Слепая модель отвечает на картинку пустой строкой и HTTP 200 — это
        # ровно тот отказ, ради которого проба и нужна.
        return False, "модель вернула пустой ответ на изображение"
    return (_VISION_DIGIT in answer), f"ответ: {answer[:40]}"


async def probe_multi_image(model_key: str, *, router: Any = None) -> tuple[bool | None, str]:
    """Читает ли модель ВСЕ переданные кадры или только первый.

    Не косметика: чтение чертежа режет плотный лист на тайлы и умеет отправить
    их одним запросом. Модель, которая смотрит только первый кадр, при этом
    молча теряет остальные — не отказом, а правдоподобным ответом по четверти
    листа. Обратная ошибка не дешевле: если считать, что модель кадров не
    берёт, плотный A1 сжимается в один кадр там, где резать было можно.

    Каталог этот флаг почти всегда наследует дефолтом `discovered`, то есть
    не знает. Измерено на стенде: `qwen3.8:27b` при двух кадрах называет
    только первую цифру — в любом порядке, хотя каждую по отдельности читает
    верно. Так что дефолт там оказался верен, но верен он был случайно.
    """
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.CAD_TEXT_OCR,
        messages=[ChatMessage(role="user", content=_MULTI_IMAGE_PROMPT)],
        images=[_digit_image(_VISION_DIGIT), _digit_image(_SECOND_DIGIT)],
        confidential=False,
        allow_cloud=True,
        preferred_model=model_key,
        thinking=False,
        max_output_tokens=24,
        inference_params={"temperature": 0},
        metadata={"format_max_reasks": 0},
    )
    try:
        response = await router.run(request)
    except Exception as exc:  # noqa: BLE001 — «не смогли спросить» ≠ «не умеет»
        logger.info(
            "capability_probe_multi_image_unavailable", model=model_key, error=str(exc)[:200]
        )
        return None, f"проба не выполнена: {type(exc).__name__}"
    if not _answered_by_the_named_model(model_key, response):
        return None, f"ответил другой кандидат цепочки ({response.model}) — проба недействительна"
    answer = (response.text or "").strip()
    if not answer:
        return None, "модель вернула пустой ответ — о кадрах это ничего не говорит"
    if _VISION_DIGIT not in answer and _SECOND_DIGIT not in answer:
        # Ни одной цифры: это про зрение, а не про число кадров.
        return None, f"ни одна цифра не названа (ответ: {answer[:40]}) — вопрос не о кадрах"
    both = _VISION_DIGIT in answer and _SECOND_DIGIT in answer
    return both, f"ответ: {answer[:40]}"


async def probe_structured(model_key: str, *, router: Any = None) -> tuple[bool | None, str]:
    """Держит ли модель строгую схему ответа."""
    from app.ai.schemas import AIRequest, AITask, ChatMessage

    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    request = AIRequest(
        task=AITask.STRUCTURED_EXTRACTION,
        messages=[ChatMessage(role="user", content=_STRUCTURED_PROMPT)],
        confidential=False,
        allow_cloud=True,
        preferred_model=model_key,
        thinking=False,
        max_output_tokens=64,
        inference_params={"temperature": 0},
        metadata={
            "json_schema": _STRUCTURED_SCHEMA,
            # Проба должна измерить модель, а не спасательный контур роутера:
            # с переспросами она бы показала, чего модель добивается со второй
            # попытки, а не что она делает сразу.
            "format_max_reasks": 0,
        },
    )
    try:
        response = await router.run(request)
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "capability_probe_structured_unavailable", model=model_key, error=str(exc)[:200]
        )
        return None, f"проба не выполнена: {type(exc).__name__}"

    if not _answered_by_the_named_model(model_key, response):
        return None, f"ответил другой кандидат цепочки ({response.model}) — проба недействительна"

    from app.ai.structured_output import parse_json_output, validate_against_schema

    payload = response.data if isinstance(response.data, dict) else None
    if payload is None:
        payload = parse_json_output(response.text or "")
    if payload is None:
        return False, "ответ не разобрался как JSON"
    problem = validate_against_schema(payload, _STRUCTURED_SCHEMA)
    if problem:
        return False, problem
    return True, "схема соблюдена"


async def probe_model(
    model_key: str,
    *,
    checks: frozenset[str] = frozenset({"vision", "structured"}),
    router: Any = None,
) -> ProbeResult:
    """Проверить модель и вернуть то, что удалось установить."""
    details: dict[str, Any] = {}
    vision = structured = multi_image = None
    if "vision" in checks:
        vision, note = await probe_vision(model_key, router=router)
        details["vision"] = note
    if "multi_image" in checks:
        # Спрашивать про кадры слепую модель бессмысленно: ответ будет про
        # зрение, а записан как факт о кадрах.
        if vision is False:
            details["multi_image"] = "пропущено: модель не видит изображений"
        else:
            multi_image, note = await probe_multi_image(model_key, router=router)
            details["multi_image"] = note
    if "structured" in checks:
        structured, note = await probe_structured(model_key, router=router)
        details["structured"] = note
    return ProbeResult(
        vision=vision,
        structured=structured,
        multi_image=multi_image,
        checked_at=datetime.now(UTC).strftime("%Y-%m-%d"),
        details=details,
    )


def apply_probe(model_key: str, result: ProbeResult, *, current_modalities: set[str]) -> None:
    """Записать результат в оверлей проверенных возможностей.

    Модальности пишутся только когда проба про них что-то УСТАНОВИЛА: список,
    собранный из «не знаем», был бы хуже отсутствующего — он выглядел бы как
    проверенный факт.
    """
    from app.ai.model_registry import set_capability_override

    modalities: list[str] | None = None
    if result.vision is not None:
        updated = set(current_modalities) | {"text"}
        if result.vision:
            updated.add("vision")
        else:
            updated.discard("vision")
        modalities = sorted(updated)

    if modalities is None and result.structured is None and result.multi_image is None:
        # Ничего не установили — записывать нечего, иначе модель получит
        # штамп «проверено» без единого проверенного факта.
        return

    set_capability_override(
        model_key,
        modalities=modalities,
        supports_structured_output=result.structured,
        supports_multi_image=result.multi_image,
        checked_at=result.checked_at,
    )
