"""Кому сейчас нужна видеокарта — и должна ли фоновая работа посторониться.

Приоритет на этом стенде задан явно: главное — агент, а студия (ComfyUI) при
генерации забирает карту целиком. Разбор каталога рядом с этим — работа на
часы, которую не жаль отложить на несколько минут.

До этого модуля выбор был ручным: человек нажимал «Приостановить», чтобы
освободить карту, и разбор потом стоял, пока о нём не вспомнят (замерено на
стенде: 1016 отрендеренных страниц простояли двенадцать часов после ручной
паузы). Автоуступка снимает и нажатие, и обязанность помнить.

Признак занятости — АКТИВНОСТЬ, а не присутствие модели в памяти. Модель
агента на этом стенде закреплена навсегда (её выгрузка назначена на 2318 год,
чтобы первый ответ был мгновенным), так что «чужая модель резидентна» верно
всегда и как сигнал бесполезно: разбор уступал бы вечно.
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

# Столько нужно, чтобы поднять модель извлечения, если её сейчас нет в памяти.
# Меньше — значит карту занял кто-то ещё (обычно студия).
MIN_FREE_VRAM_GB = 7.0
# Сколько после хода агента считать, что человек ждёт продолжения разговора.
INTERACTIVE_WINDOW_SECONDS = 90
_INTERACTIVE_KEY = "gpu:interactive_until"
# На сколько откладывается партия. Совпадает с ритмом resume_stalled: если
# отложенная задача почему-то потеряется, её подберёт возобновление.
YIELD_MINUTES = 5
# Уступать — да, уступать бесконечно — нет. Модель агента висит в памяти пять
# минут после ЛЮБОГО обращения, так что редкой фоновой активности хватило бы,
# чтобы разбор не сдвинулся никогда. После получаса ожидания партия проходит
# в любом случае: одна страница задержит агента на секунды, а вставший навсегда
# каталог — это работа, которая не будет сделана.
MAX_CONSECUTIVE_YIELDS = 6
_YIELD_KEY = "catalog:gpu_yield:{document_id}"
_YIELD_COUNT_KEY = "catalog:gpu_yield_count:{document_id}"


def _redis():
    from app.utils.redis_client import get_sync_redis

    return get_sync_redis()


async def gpu_yield_reason(our_model: str | None = None) -> str | None:
    """Почему фоновой обработке стоит подождать. None — можно работать.

    Best-effort: любая ошибка проверки means «работаем» — фоновая задача не
    должна вставать из-за недоступной телеметрии.
    """
    # 1. Эксклюзивный захват карты (обучение LoRA) — здесь спорить не о чем.
    try:
        from app.ai import gpu_lock

        if gpu_lock.is_locked():
            return "идёт обучение LoRA"
    except Exception as exc:  # noqa: BLE001
        logger.debug("gpu_courtesy_lock_check_failed", error=str(exc)[:120])

    # None — Ollama не ответила (о карте ничего не известно), [] — ответила и
    # ничего не держит. Смешивать нельзя: в первом случае судить по свободной
    # памяти неправильно, наша модель вполне может быть в ней.
    loaded: list[str] | None = None
    try:
        import httpx

        from app.config import settings

        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.ollama_url}/api/ps")
        response.raise_for_status()
        loaded = [str(m.get("name") or "") for m in (response.json().get("models") or [])]
    except Exception as exc:  # noqa: BLE001 — недоступность Ollama решается не здесь
        logger.debug("gpu_courtesy_ps_failed", error=str(exc)[:120])

    # 2. Человек прямо сейчас разговаривает с агентом.
    if is_interactive():
        return "агент отвечает человеку"

    # 3. Нашей модели в памяти нет, и места под неё не осталось: обычно это
    #    студия, которая забирает карту целиком.
    if loaded == []:
        try:
            from app.ai.gpu_manager import get_gpu_telemetry

            telemetry = await get_gpu_telemetry()
            free = getattr(telemetry, "vram_free_gb", None) if telemetry else None
            if free is not None and free < MIN_FREE_VRAM_GB:
                return f"на карте свободно {free:.1f} ГБ — занята другой задачей"
        except Exception as exc:  # noqa: BLE001
            logger.debug("gpu_courtesy_telemetry_failed", error=str(exc)[:120])

    return None


def mark_interactive(seconds: int = INTERACTIVE_WINDOW_SECONDS) -> None:
    """Агент начал ход — фоновой работе стоит подождать эти секунды.

    Ставит сам агент: это единственный источник, который точно знает, что
    ответа ждёт человек. По состоянию памяти этого не увидеть — модель агента
    закреплена и висит в ней всегда.
    """
    try:
        _redis().setex(_INTERACTIVE_KEY, seconds, "1")
    except Exception as exc:  # noqa: BLE001 — отметка не данные
        logger.debug("gpu_courtesy_interactive_mark_failed", error=str(exc)[:120])


def is_interactive() -> bool:
    try:
        return bool(_redis().exists(_INTERACTIVE_KEY))
    except Exception:  # noqa: BLE001
        return False


def yields_exhausted(document_id: str) -> bool:
    """Уступали слишком долго — пора пропустить партию вперёд."""
    try:
        count = _redis().get(_YIELD_COUNT_KEY.format(document_id=document_id))
        return int(count or 0) >= MAX_CONSECUTIVE_YIELDS
    except Exception:  # noqa: BLE001
        return False


def mark_yielded(document_id: str, minutes: int = YIELD_MINUTES) -> None:
    """Пометить, что по документу уже стоит отложенная задача.

    Без этого `catalog.resume_stalled` увидел бы «страницы ждут, задач нет» и
    поставил вторую цепочку — две партии на один каталог.
    """
    try:
        client = _redis()
        client.setex(_YIELD_KEY.format(document_id=document_id), minutes * 60 + 30, "1")
        # Счётчик живёт дольше самой отметки: он считает уступки ПОДРЯД, и
        # сбрасывается, только когда партия действительно прошла.
        counter = _YIELD_COUNT_KEY.format(document_id=document_id)
        client.incr(counter)
        client.expire(counter, (minutes * 60 + 30) * (MAX_CONSECUTIVE_YIELDS + 2))
    except Exception as exc:  # noqa: BLE001 — пометка не данные
        logger.debug("gpu_courtesy_mark_failed", error=str(exc)[:120])


def is_yielded(document_id: str) -> bool:
    """Стоит ли по документу отложенная задача (тогда его не трогают)."""
    try:
        return bool(_redis().exists(_YIELD_KEY.format(document_id=document_id)))
    except Exception:  # noqa: BLE001
        return False


def clear_yield(document_id: str) -> None:
    """Партия прошла — и отметка, и счётчик подряд идущих уступок сбрасываются."""
    try:
        client = _redis()
        client.delete(_YIELD_KEY.format(document_id=document_id))
        client.delete(_YIELD_COUNT_KEY.format(document_id=document_id))
    except Exception:  # noqa: BLE001
        pass
