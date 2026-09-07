"""Один контракт для «текста, прочитанного с листа», каким бы ни был источник.

В проекте одновременно жили два несвязанных текстовых слоя.

Метод «по описанию» просил vision-модель «прочитать все надписи и размеры» и
разбирал ОТВЕТ ПОСТРОЧНО регулярками. Модель отвечала markdown-отчётом, и в
список размеров попадали строки целиком, вместе с ``**`` и дефисами, а вместе с
ними — догадки вида ``**185** — общая длина вала (вероятно)``. Координат в этом
представлении не было вовсе, поэтому привязать значение к месту на листе было
нечем: ни подсветить в интерфейсе, ни свериться с геометрией.

Растровая трассировка тем временем уже умела правильно: ``vlm_dimensions.
read_sheet_text_entities`` тайлит лист, просит СТРОГИЙ JSON ``[{"text","bbox"}]``
и переводит координаты обратно в пиксели листа. Пользовался этим только один
путь из двух.

Здесь эти два слоя сводятся к одному контракту. Источник может быть любым:

* **движок** (tesseract) — bbox и уверенность отдаёт сам;
* **vision-LLM с координатами** — её просят вернуть их в JSON;
* **vision-LLM без координат** — ``bbox=None`` и ``grounded=False``.

Третий случай — не ошибка и не повод молча потерять слой; это факт о модели,
который должен быть виден. Решение оператора при постановке задачи: «не смог —
``bbox: null``, и это видно».
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

# Строки, которые описывают ЛИСТ, а не деталь: формат, масштаб, номер листа.
# Они читаются штампом отдельно и в списке размеров только мешают.
_SHEET_METADATA_LINE = re.compile(
    r"^\s*(?:NIST|PMI\s+(?:test|complex)|sheet|page|revision|rev\b"
    r"|лист|страниц|ревиз|формат|масштаб|дата)\b",
    re.IGNORECASE,
)

# Разметка markdown-отчёта: модель, которую попросили «прочитать надписи»,
# отвечает заголовками и буллетами, а не голыми строками.
_MARKDOWN_NOISE = re.compile(r"^[\s>#*\-–—•]+|[\s*_`]+$")

# Заголовок раздела отчёта («## Основная надпись (штамп):») — структура ответа
# модели, а не надпись с чертежа. Снять решётки мало: текст заголовка тогда
# остаётся в слое и выглядит как прочитанное.
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s")

# Оговорка модели о собственной неуверенности. Такое значение — не прочитанное
# с листа, а предположенное, и размером оно быть не может.
_HEDGE = re.compile(
    r"\(?\s*(?:вероятно|возможно|примерно|ориентировочно|скорее всего)\s*\)?", re.IGNORECASE
)

_ANNOTATION_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("roughness", re.compile(r"\bR[az]\s*\d", re.IGNORECASE)),
    ("hardness", re.compile(r"\bHRC|\bHB\b|твёрд|тверд", re.IGNORECASE)),
    ("thread", re.compile(r"\bM\d+\s*[x×]", re.IGNORECASE)),
    ("material", re.compile(r"сталь|чугун|бронз|латун|алюмин", re.IGNORECASE)),
)


class TextToken(BaseModel):
    """Одна прочитанная надпись."""

    text: str
    # Пиксели ПОЛНОГО листа: [x1, y1, x2, y2]. None — источник координат не дал.
    bbox: list[float] | None = None
    confidence: float | None = None
    orientation: Literal["normal", "vertical"] = "normal"
    source: Literal["engine", "vlm", "vlm_ungrounded"] = "vlm_ungrounded"


class TextLayer(BaseModel):
    """Текстовый слой листа целиком плюс то, чем он прочитан."""

    tokens: list[TextToken] = Field(default_factory=list)
    # Ответ источника как есть — для аудита и повторного разбора.
    raw_text: str = ""
    model: str = ""
    source: Literal["engine", "vlm", "vlm_ungrounded", "none"] = "none"

    @property
    def grounded(self) -> bool:
        """Есть ли координаты хотя бы у одного токена."""
        return any(token.bbox is not None for token in self.tokens)

    def as_callouts(self) -> dict[str, list[dict]]:
        """Производные ``dimensions``/``annotations`` для контракта спека.

        Считаются ДЕТЕРМИНИРОВАННО из токенов. Раньше это делалось из строк
        markdown, поэтому в размеры попадала разметка и оговорки модели.
        """
        dimensions: list[dict] = []
        annotations: list[dict] = []
        for token in self.tokens:
            text = token.text
            kind = _annotation_kind(text)
            if kind:
                annotations.append({"kind": kind, "text": text[:200], "bbox": token.bbox})
            elif _looks_like_dimension(text):
                dimensions.append({"value": text[:60], "applies_to": None, "bbox": token.bbox})
        return {"dimensions": dimensions, "annotations": annotations}


def _annotation_kind(text: str) -> str | None:
    for kind, pattern in _ANNOTATION_KINDS:
        if pattern.search(text):
            return kind
    # Строка со ссылкой на стандарт — надпись, а не размер: она несёт номер и
    # год, которые иначе попадут в пул чисел, из которого выбираются диаметры и
    # осевые станции.
    if _STANDARD_CITATION.search(text):
        return "other"
    return None


# Ссылка на стандарт — не размер, каким бы дефисом её ни записали и как бы OCR
# ни исказил соседние слова. Живой прогон: «Смаль 45 ГОСТ 1050-2014» — «Сталь»,
# прочитанное с опечаткой, — не попало в материалы по словарю и уехало в список
# размеров вместе с номером и годом стандарта.
_STANDARD_CITATION = re.compile(r"\b(?:ГОСТ|ОСТ|СТП|ТУ|ISO|DIN|EN|ANSI|ASME)\b", re.IGNORECASE)


def _looks_like_dimension(text: str) -> bool:
    if _STANDARD_CITATION.search(text):
        return False
    return bool(re.search(r"\d", text)) and len(text) <= 60


def clean_token_text(raw: str) -> str:
    """Надпись без markdown-разметки и без оговорок модели.

    ``- **185** — общая длина вала (вероятно)`` — это не размер, прочитанный с
    листа: число там есть, но модель сама пометила его догадкой. Такие строки
    отбрасываются целиком, а не чистятся до числа.
    """
    if _MARKDOWN_HEADING.match(raw or ""):
        return ""
    text = _MARKDOWN_NOISE.sub("", (raw or "").strip())
    text = text.replace("**", "").strip()
    if _HEDGE.search(text):
        return ""
    return text.strip(" \t—–-:")


def is_sheet_metadata(text: str) -> bool:
    return bool(_SHEET_METADATA_LINE.match(text or ""))


# ── Адаптеры источников ──────────────────────────────────────────────────────


def layer_from_engine(observations: list[dict[str, Any]], *, model: str) -> TextLayer:
    """Токены OCR-движка (tesseract) в общий контракт.

    ``_rotated_numeric_tokens`` в ``diameter_dimensions`` уже отдаёт ровно эти
    поля — ``raw_text``/``ocr_confidence``/``orientation``/``label_bbox``, — так
    что здесь только переименование, а не второй разбор.
    """
    tokens: list[TextToken] = []
    for item in observations or []:
        text = clean_token_text(str(item.get("raw_text") or item.get("text") or ""))
        if not text or is_sheet_metadata(text):
            continue
        bbox = item.get("label_bbox") or item.get("bbox")
        tokens.append(
            TextToken(
                text=text,
                bbox=[float(v) for v in bbox] if _is_bbox(bbox) else None,
                confidence=_as_confidence(item.get("ocr_confidence", item.get("confidence"))),
                orientation="vertical" if item.get("orientation") == "vertical" else "normal",
                source="engine",
            )
        )
    return TextLayer(tokens=_dedup(tokens), model=model, source="engine")


def layer_from_vlm_json(
    records: list[Any],
    *,
    model: str,
    raw_text: str = "",
    image_size: tuple[int, int] | None = None,
    normalized_scale: float | None = None,
) -> TextLayer:
    """Ответ vision-модели в строгой форме ``[{"text","bbox"}]``.

    ``normalized_scale`` задаётся, когда модель отдаёт координаты в своей
    нормализованной сетке (у qwen3-vl это 0..1000) — тогда ``image_size``
    переводит их обратно в пиксели листа.
    """
    tokens: list[TextToken] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        text = clean_token_text(str(record.get("text") or ""))
        if not text or is_sheet_metadata(text):
            continue
        # `bbox_2d` — собственное имя поля у qwen3-vl; просить её отвечать
        # «bbox» бесполезно, она всё равно пишет своё.
        raw_box = record.get("bbox")
        if not _is_bbox(raw_box):
            raw_box = record.get("bbox_2d")
        bbox = _rescaled_bbox(raw_box, image_size, normalized_scale)
        tokens.append(
            TextToken(
                text=text,
                bbox=bbox,
                confidence=_as_confidence(record.get("confidence")),
                source="vlm" if bbox else "vlm_ungrounded",
            )
        )
    tokens = _dedup(tokens)
    grounded = any(token.bbox is not None for token in tokens)
    return TextLayer(
        tokens=tokens,
        raw_text=raw_text,
        model=model,
        source="vlm" if grounded else "vlm_ungrounded",
    )


# Модель, зациклившаяся на промпте, повторяет одну строку десятками. Это не
# содержимое листа, а вырождение ответа, и в слое ему не место.
_MAX_REPEATS = 2


def _json_fragments(raw: str) -> list[dict[str, Any]]:
    """Все JSON-объекты из ответа — массивом, россыпью или в ```-ограждениях.

    Формы, встреченные вживую на одном и том же запросе:

    * ``[{"text": ..., "bbox": [...]}, ...]`` — то, о чём просили;
    * несколько отдельных ```json {"text": "строка\nстрока"} ``` подряд —
      так отвечает glm-ocr, документная модель на 1.1B: массив она собрать не
      может, но текст читает верно;
    * проза без единой скобки.

    Разбирать только первую форму значило терять две остальные целиком: на
    живом прогоне из богатой транскрипции штампа доезжало пять мусорных
    токенов, потому что фигурные скобки и кавычки разбирались как строки.
    """
    import json

    text = (raw or "").strip()
    out: list[dict[str, Any]] = []

    try:
        value = json.loads(text)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]
    except Exception:  # noqa: BLE001 — дальше разбираем по кускам
        pass

    # Массив целиком, если он утонул в пояснениях.
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        except Exception:  # noqa: BLE001
            pass

    # Россыпь объектов: балансируем скобки и разбираем каждый отдельно.
    depth, buffer = 0, []
    for char in text:
        if char == "{":
            depth += 1
        if depth:
            buffer.append(char)
        if char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads("".join(buffer))
                    if isinstance(value, dict):
                        out.append(value)
                except Exception:  # noqa: BLE001 — кусок оказался не JSON
                    pass
                buffer = []
    return out


def _drop_degenerate(tokens: list[TextToken], prompt: str = "") -> list[TextToken]:
    """Убрать эхо промпта и зацикленные повторы.

    Модель, которой задали непосильный вопрос, повторяет его же формулировку —
    на живом прогоне glm-ocr выдала «Все надписи и размеры с этого чертежа»
    четырнадцать раз подряд. Принять это за прочитанное с листа нельзя.
    """
    prompt_words = {w for w in re.split(r"\W+", (prompt or "").lower()) if len(w) > 3}
    seen: dict[str, int] = {}
    kept: list[TextToken] = []
    for token in tokens:
        key = token.text.lower()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > _MAX_REPEATS:
            continue
        words = {w for w in re.split(r"\W+", key) if len(w) > 3}
        # Строка, целиком состоящая из слов промпта, — эхо, а не надпись.
        if words and prompt_words and words <= prompt_words:
            continue
        kept.append(token)
    return kept


def layer_from_answer(
    raw_text: str,
    *,
    model: str,
    prompt: str = "",
    image_size: tuple[int, int] | None = None,
    normalized_scale: float | None = None,
) -> TextLayer:
    """Ответ ЛЮБОЙ модели — в общий контракт, какой бы формы он ни был.

    Единая точка входа: сначала пробуем разобрать структуру (с координатами или
    без), затем — прозу. Что удалось, записано в ``source``/``grounded``, а не
    выясняется потерей значений ниже по конвейеру.
    """
    fragments = _json_fragments(raw_text)
    if fragments:
        tokens: list[TextToken] = []
        for record in fragments:
            raw_box = record.get("bbox")
            if not _is_bbox(raw_box):
                raw_box = record.get("bbox_2d")
            bbox = _rescaled_bbox(raw_box, image_size, normalized_scale)
            # `text` может нести сразу несколько строк — так отвечает документная
            # модель, у которой на объект приходится целый блок штампа.
            for line in str(record.get("text") or "").splitlines():
                text = clean_token_text(line)
                if not text or is_sheet_metadata(text):
                    continue
                tokens.append(
                    TextToken(
                        text=text,
                        bbox=bbox,
                        confidence=_as_confidence(record.get("confidence")),
                        source="vlm" if bbox else "vlm_ungrounded",
                    )
                )
        tokens = _drop_degenerate(_dedup(tokens), prompt)
        if tokens:
            grounded = any(token.bbox is not None for token in tokens)
            return TextLayer(
                tokens=tokens,
                raw_text=raw_text or "",
                model=model,
                source="vlm" if grounded else "vlm_ungrounded",
            )

    layer = layer_from_prose(raw_text, model=model)
    return layer.model_copy(update={"tokens": _drop_degenerate(layer.tokens, prompt)})


def layer_from_prose(raw_text: str, *, model: str) -> TextLayer:
    """Свободный текст модели, которая не смогла вернуть координаты.

    Худший из трёх источников и единственный, который был до этой правки.
    Координат нет — и это записано в самом слое (``grounded == False``), а не
    выясняется потерей значений где-то ниже по конвейеру.
    """
    tokens: list[TextToken] = []
    for line in (raw_text or "").splitlines():
        text = clean_token_text(line)
        if not text or is_sheet_metadata(text):
            continue
        tokens.append(TextToken(text=text, source="vlm_ungrounded"))
    return TextLayer(
        tokens=_dedup(tokens),
        raw_text=raw_text or "",
        model=model,
        source="vlm_ungrounded",
    )


# ── Вспомогательное ──────────────────────────────────────────────────────────


def _is_bbox(value: Any) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    )


def _rescaled_bbox(
    value: Any, image_size: tuple[int, int] | None, normalized_scale: float | None
) -> list[float] | None:
    if not _is_bbox(value):
        return None
    box = [float(v) for v in value]
    if normalized_scale and image_size:
        width, height = image_size
        sx, sy = width / normalized_scale, height / normalized_scale
        box = [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy]
    return box


def _as_confidence(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    # Движки публикуют уверенность в процентах, модели — долей.
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _dedup(tokens: list[TextToken]) -> list[TextToken]:
    """Модель зацикливается и повторяет строки; координаты у повтора разные.

    Первое вхождение выигрывает — если у него есть bbox. Токен с координатами
    всегда лучше такого же без них, поэтому повтор может заменить предыдущий.
    """
    by_text: dict[str, TextToken] = {}
    order: list[str] = []
    for token in tokens:
        key = token.text.lower()
        existing = by_text.get(key)
        if existing is None:
            by_text[key] = token
            order.append(key)
        elif existing.bbox is None and token.bbox is not None:
            by_text[key] = token
    return [by_text[key] for key in order]
