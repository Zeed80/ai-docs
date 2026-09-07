"""Structured output enforcement — JSON extraction and repair for weak LLMs.

Pipeline for any model output:
  1. Extract JSON from raw text (strip markdown, prose preamble)
  2. Validate against Pydantic schema or JSONSchema dict
  3. On failure: send a correction prompt and retry
  4. On repeated failure: return schema-minimal defaults

Weak local models often wrap JSON in prose or markdown — this module handles all that.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# ── JSON extraction ────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_BRACE_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)
_BRACKET_RE = re.compile(r"\[[\s\S]*\]", re.DOTALL)


def extract_json_from_text(text: str) -> str | None:
    """Extract the first JSON object or array from arbitrary LLM output."""
    text = text.strip()

    # 1. Markdown fence: ```json ... ```
    m = _FENCE_RE.search(text)
    if m:
        candidate = m.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 2. Bare JSON object
    m = _BRACE_RE.search(text)
    if m:
        candidate = m.group(0).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 3. Bare JSON array
    m = _BRACKET_RE.search(text)
    if m:
        candidate = m.group(0).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # 4. Try repairing common issues in the whole text
    repaired = _repair_json(text)
    if repaired:
        return repaired

    return None


def _repair_json(text: str) -> str | None:
    """Attempt lightweight JSON repair: trailing commas, single quotes, unquoted keys.

    Порядок здесь важен. Замена одинарных кавычек на двойные — самая грубая из
    починок: она портит валидный JSON с апострофом внутри строки, а на русском
    тексте и в дюймовых обозначениях (``1'``) апостроф встречается постоянно.
    Поэтому она идёт ПОСЛЕДНЕЙ, когда всё остальное уже не помогло, а не первой,
    как раньше.
    """

    def _first_json(candidate_text: str) -> str | None:
        for match in (_BRACE_RE.search(candidate_text), _BRACKET_RE.search(candidate_text)):
            if match:
                try:
                    candidate = match.group(0)
                    json.loads(candidate)
                    return candidate
                except Exception:  # noqa: BLE001 — пробуем следующий вариант
                    pass
        return None

    # 1. Только висячие запятые — безопасная правка.
    without_trailing = re.sub(r",\s*([\}\]])", r"\1", text)
    repaired = _first_json(without_trailing)
    if repaired is not None:
        return repaired

    # 2. Последнее средство: одинарные кавычки как строковые.
    return _first_json(re.sub(r"'([^']*)'", r'"\1"', without_trailing))


def parse_json_output(text: str, default: Any = None) -> Any:
    """Extract and parse JSON from LLM output. Returns default on failure."""
    raw = extract_json_from_text(text or "")
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


# ── Schema-enforced generation with retry ────────────────────────────────────

_CORRECTION_TEMPLATE = """\
Your previous response could not be parsed as valid JSON.
Error: {error}

Please respond ONLY with a valid JSON object matching this schema (no markdown, no prose):
{schema}

Your corrected response:"""

_SCHEMA_INJECT_TEMPLATE = """\
{original_prompt}

IMPORTANT: Respond ONLY with a valid JSON object. No markdown. No explanations. No preamble.
Use exactly this structure:
{schema_example}"""


async def enforce_structured_output(
    prompt: str,
    schema: type[T] | dict[str, Any],
    generate_fn: Callable[[str], Coroutine[Any, Any, str]],
    *,
    max_retries: int = 3,
    inject_schema: bool = True,
) -> T | dict | None:
    """Call generate_fn with JSON enforcement. Returns parsed + validated result.

    Args:
        prompt: The user/task prompt.
        schema: Pydantic model class OR a dict example/schema.
        generate_fn: async callable (prompt: str) -> str  (raw LLM output).
        max_retries: Number of correction attempts.
        inject_schema: Whether to inject schema hint into the initial prompt.
    """
    schema_str = _schema_to_str(schema)
    active_prompt = (
        _SCHEMA_INJECT_TEMPLATE.format(original_prompt=prompt, schema_example=schema_str)
        if inject_schema
        else prompt
    )

    last_error: str = ""
    for attempt in range(max_retries):
        raw = await generate_fn(active_prompt)
        parsed = parse_json_output(raw)

        if parsed is not None:
            try:
                return _validate(parsed, schema)
            except Exception as e:
                last_error = str(e)
        else:
            last_error = "Could not extract JSON from response"

        logger.warning(
            "structured_output_retry",
            attempt=attempt + 1,
            max_retries=max_retries,
            error=last_error,
        )
        # Correction prompt for next attempt
        active_prompt = _CORRECTION_TEMPLATE.format(error=last_error, schema=schema_str)

    logger.error("structured_output_failed_all_retries", error=last_error)
    return _defaults(schema)


def _schema_to_str(schema: type[T] | dict) -> str:
    if isinstance(schema, dict):
        return json.dumps(schema, ensure_ascii=False, indent=2)
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        example = {}
        for name, field in schema.model_fields.items():
            ann = field.annotation
            example[name] = _type_example(ann)
        return json.dumps(example, ensure_ascii=False, indent=2)
    return "{}"


def _type_example(annotation: Any) -> Any:
    origin = getattr(annotation, "__origin__", None)
    if annotation is str or annotation == "str":
        return "<string>"
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0
    if annotation is bool:
        return False
    if origin is list:
        return []
    if origin is dict:
        return {}
    return None


def _validate(data: Any, schema: type[T] | dict) -> T | dict:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema.model_validate(data)
    return data  # dict schema — return parsed dict as-is


def _defaults(schema: type[T] | dict) -> T | dict | None:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            return schema.model_construct()
        except Exception:
            return None
    return {}


def schema_to_str(schema: type[BaseModel] | dict | None) -> str:
    """Схема человекочитаемой строкой. Публичная обёртка над `_schema_to_str`."""
    if schema is None:
        return "{}"
    return _schema_to_str(schema)


def correction_prompt(error: str, schema: type[BaseModel] | dict | None) -> str:
    """Просьба исправить ответ, названная ошибкой валидации.

    Шаблон существовал внутри `enforce_structured_output`, у которой контракт
    ``generate_fn(prompt: str) -> str``: она теряет messages, картинки и
    параметры вывода, поэтому для читателя чертежа не годится вовсе. Сам текст
    просьбы при этом хорош и переиспользуется роутером, который переспрашивает
    ДОБАВЛЕННЫМИ сообщениями, ничего не теряя.
    """
    return _CORRECTION_TEMPLATE.format(error=error, schema=schema_to_str(schema))


_TYPE_CHECKS: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _declared_types(node: dict) -> set[str]:
    """Объявленные типы поля.

    В JSON Schema ``type`` — это строка ИЛИ список: ``["string", "null"]`` для
    необязательного поля пишется именно так, и Pydantic генерирует такие схемы
    сам. Использовать это значение ключом словаря нельзя — список не хешируется,
    и живой прогон падал ровно на этом: `TypeError: unhashable type: 'list'`
    десять раз подряд, по разу на каждый фрагментный вопрос.
    """
    raw = node.get("type")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {item for item in raw if isinstance(item, str)}
    return set()


def validate_against_schema(payload: Any, schema: dict) -> str | None:
    """Лёгкая проверка ответа против JSON-схемы; ``None`` — претензий нет.

    Нужна там, где схема пришла словарём (`metadata["json_schema"]` — так
    работает весь `cad_recognize`): такие ответы роутер не проверял ВООБЩЕ, и
    негодный ответ уходил вызывающему как успех, без единого шанса на переспрос.

    Намеренно неполная: без внешней зависимости проверяются тип верхнего
    уровня, наличие обязательных ключей и типы их значений. Этого хватает,
    чтобы отличить «модель ответила не тем» от «модель ответила тем»; полную
    валидацию делает вызывающий своей Pydantic-моделью.
    """
    expected = _declared_types(schema)
    if expected == {"array"} and not isinstance(payload, list):
        return "ожидался JSON-массив"
    if expected == {"object"} and not isinstance(payload, dict):
        return "ожидался объект JSON"
    if not isinstance(payload, dict):
        return None

    required = schema.get("required")
    if isinstance(required, list):
        missing = [str(key) for key in required if key not in payload]
        if missing:
            return f"нет обязательных полей: {', '.join(missing)}"

    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    for key, spec in properties.items():
        if key not in payload or not isinstance(spec, dict):
            continue
        value = payload[key]
        if value is None:
            continue
        declared = _declared_types(spec)
        allowed = tuple(_TYPE_CHECKS[name] for name in declared if name in _TYPE_CHECKS)
        if not allowed:
            continue
        # bool — подкласс int в Python, но не целое в JSON Schema.
        if isinstance(value, bool) and "boolean" not in declared:
            return f"поле «{key}»: ожидалось {'/'.join(sorted(declared))}, пришло логическое"
        flat: tuple = tuple(
            item for entry in allowed for item in (entry if isinstance(entry, tuple) else (entry,))
        )
        if not isinstance(value, flat):
            return f"поле «{key}»: ожидался тип {'/'.join(sorted(declared))}"
    return None
