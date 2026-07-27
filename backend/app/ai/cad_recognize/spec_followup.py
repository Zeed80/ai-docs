"""Ask about the one value that is missing, instead of refusing the sheet.

A read that came back with nine of a shaft's ten step lengths used to stop the
whole redraw: ``unresolved`` is fail-closed, and one hole in the profile means
no part at all. But "I could not read this" is not the end of a conversation —
it is a question, and it is a far EASIER question than the one that failed. The
reader was asked to describe an entire stepped contour off a dense A3 sheet;
here it is asked for a single axial length, with the neighbouring diameters
named and the sheet's own axial callouts listed in front of it.

Two rules keep this from becoming a guessing machine:

* An answer is accepted only if the number appears among the callouts the sheet
  was already read to carry. A follow-up cannot introduce a dimension that is
  nowhere on the drawing — that is what the assumption layer is for, and it
  labels its values as assumed.
* Nothing is repaired silently. Every question, its answer and the reason it was
  accepted or rejected travel back with the spec, so a reviewer sees which
  numbers came from a second look rather than from the first read.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# One narrow question. The contract is a single number, so it cannot truncate
# and cannot mis-nest — the two failure modes that cost whole sheets before.
_LENGTH_PROMPT = (
    "Перед тобой главный вид детали. Нужен ОДИН размер.\n"
    "Ступень: {position}, диаметр Ø{diameter:g} мм.\n"
    "{context}"
    "Осевые размеры, уже прочитанные с этого листа: {callouts}\n\n"
    "Какова ОСЕВАЯ ДЛИНА именно этой ступени в миллиметрах? Если на чертеже "
    "дана цепочка размеров от торца — вычисли разность соседних значений. "
    "Не выдумывай: если длину этой ступени определить нельзя, верни null.\n"
    'Ответь ОДНОЙ строкой JSON: {{"length_mm": 0}} или {{"length_mm": null}}'
)

_DIAMETER_PROMPT = (
    "Перед тобой главный вид детали. Нужен ОДИН размер.\n"
    "Ступень: {position}.\n"
    "{context}"
    "Диаметры, уже прочитанные с этого листа: {callouts}\n\n"
    "Каков ДИАМЕТР именно этой ступени в миллиметрах? Не выдумывай: если "
    "определить нельзя, верни null.\n"
    'Ответь ОДНОЙ строкой JSON: {{"diameter_mm": 0}} или {{"diameter_mm": null}}'
)

_THICKNESS_PROMPT = (
    "Перед тобой плоская деталь (пластина или фланец). Нужен ОДИН размер: "
    "ТОЛЩИНА. Её видно на виде сбоку или в разрезе.\n"
    "Размеры, уже прочитанные с этого листа: {callouts}\n\n"
    "Какова толщина детали в миллиметрах? Не выдумывай: если на листе нет вида "
    "сбоку или разреза, верни null.\n"
    'Ответь ОДНОЙ строкой JSON: {{"thickness_mm": 0}} или {{"thickness_mm": null}}'
)

_NUMBER_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": ["number", "null"]}},
}


@dataclass
class FollowupAnswer:
    """One question asked about one missing value, and what came of it."""

    path: str
    field: str
    question: str
    answer: float | None = None
    accepted: bool = False
    reason: str = ""
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "field": self.field,
            "question": self.question,
            "answer_mm": self.answer,
            "accepted": self.accepted,
            "reason": self.reason,
        }


def _bodies(spec: dict) -> list[tuple[str, dict]]:
    """(path, body) for the main view and every additional part."""
    result: list[tuple[str, dict]] = []
    main = spec.get("main_view")
    if isinstance(main, dict):
        result.append(("main_view", main))
    for index, part in enumerate(spec.get("parts") or []):
        if isinstance(part, dict):
            result.append((f"parts.{index}", part))
    return result


def _missing_values(spec: dict) -> list[tuple[str, str, dict, dict]]:
    """Every value the drafter needs and the read did not supply.

    Returns ``(path, field, node, body)`` so the caller can both ASK about the
    value in its own terms and write the answer back where it belongs.
    """
    gaps: list[tuple[str, str, dict, dict]] = []
    for body_path, body in _bodies(spec):
        for group in ("outer", "bore"):
            for index, section in enumerate(body.get(group) or []):
                if not isinstance(section, dict):
                    continue
                path = f"{body_path}.{group}.{index}"
                if not _positive(section.get("diameter_mm")):
                    gaps.append((path, "diameter_mm", section, body))
                if not _positive(section.get("length_mm")):
                    gaps.append((path, "length_mm", section, body))
        profile = body.get("profile")
        if isinstance(profile, dict) and profile.get("shape"):
            if not _positive(profile.get("thickness_mm")):
                gaps.append((f"{body_path}.profile", "thickness_mm", profile, body))
    return gaps


def _positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _neighbour_context(body: dict, group: str, index: int) -> str:
    """Name the steps on either side, so the question points at one place.

    "The length of step 3" means nothing to a model looking at a drawing; "the
    step between Ø40 and Ø60" is a place on the sheet it can find.
    """
    sections = [s for s in (body.get(group) or []) if isinstance(s, dict)]
    parts: list[str] = []
    if 0 < index < len(sections):
        left = sections[index - 1].get("diameter_mm")
        if _positive(left):
            parts.append(f"слева от неё ступень Ø{float(left):g} мм")
    if 0 <= index + 1 < len(sections):
        right = sections[index + 1].get("diameter_mm")
        if _positive(right):
            parts.append(f"справа от неё ступень Ø{float(right):g} мм")
    if index == 0:
        parts.append("это первая ступень от левого торца")
    return ("Соседи: " + ", ".join(parts) + ".\n") if parts else ""


def _position_label(group: str, index: int, total: int) -> str:
    where = "наружного контура" if group == "outer" else "расточки"
    return f"{index + 1}-я из {total} {where} слева направо"


async def resolve_missing_dimensions(
    image_bytes: bytes,
    spec: dict,
    *,
    router: Any | None = None,
    confidential: bool = True,
    max_questions: int = 6,
) -> tuple[dict, list[dict]]:
    """Ask about each missing dimension; accept only what the sheet supports.

    Returns the (possibly) completed spec and the log of what was asked. The
    input spec is never mutated: the "before" half is what makes the follow-up
    auditable, and a caller comparing them can see exactly what a second look
    added.
    """
    import io

    from PIL import Image

    from app.ai.cad_recognize.spec_fragments import (
        _ask,
        _callout_numbers,
        _dominant_view_crop,
        _main_view_crop,
        _matches_callout,
        _overview,
    )

    gaps = _missing_values(spec)
    if not gaps:
        return spec, []
    if router is None:
        from app.ai.router import ai_router

        router = ai_router
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001 — no image, no follow-up, no crash
        return spec, []

    callouts = {
        "dimensions": [d for d in (spec.get("dimensions") or []) if isinstance(d, dict)]
    }
    linear_seen = _callout_numbers(callouts, "linear")
    diameters_seen = _callout_numbers(callouts, "diameter")
    shaft_view = _overview(_dominant_view_crop(image))
    plate_view = _overview(_main_view_crop(image))

    updated = copy.deepcopy(spec)
    updated_gaps = _missing_values(updated)
    log: list[FollowupAnswer] = []

    for (path, name, _node, body), (_p, _f, target, _b) in zip(
        gaps[:max_questions], updated_gaps[:max_questions], strict=False
    ):
        group = path.rsplit(".", 2)[-2] if name != "thickness_mm" else "profile"
        index = int(path.rsplit(".", 1)[-1]) if name != "thickness_mm" else 0
        total = len(body.get(group) or []) if name != "thickness_mm" else 0

        if name == "length_mm":
            diameter = target.get("diameter_mm")
            if not _positive(diameter):
                continue  # ask for the diameter first; a nameless step is not a question
            prompt = _LENGTH_PROMPT.format(
                position=_position_label(group, index, total),
                diameter=float(diameter),
                context=_neighbour_context(body, group, index),
                callouts=", ".join(f"{v:g}" for v in linear_seen[:24]) or "—",
            )
            view, allowed, key = shaft_view, linear_seen, "length_mm"
        elif name == "diameter_mm":
            prompt = _DIAMETER_PROMPT.format(
                position=_position_label(group, index, total),
                context=_neighbour_context(body, group, index),
                callouts=", ".join(f"{v:g}" for v in diameters_seen[:24]) or "—",
            )
            view, allowed, key = shaft_view, diameters_seen, "diameter_mm"
        else:
            prompt = _THICKNESS_PROMPT.format(
                callouts=", ".join(f"{v:g}" for v in linear_seen[:24]) or "—",
            )
            view, allowed, key = plate_view, linear_seen, "thickness_mm"

        entry = FollowupAnswer(path=path, field=name, question=prompt.split("\n\n")[0])
        answer = await _ask(
            prompt, view, router=router, confidential=confidential,
            num_predict=200, schema=_NUMBER_SCHEMA,
        )
        value = answer.get(key) if isinstance(answer, dict) else None
        if not _positive(value):
            entry.reason = "модель не смогла прочитать этот размер"
            log.append(entry)
            continue
        entry.answer = float(value)
        if allowed and not _matches_callout(float(value), allowed):
            # The follow-up may only RECOVER a number the sheet carries. A value
            # that appears nowhere among the callouts is the model filling in a
            # blank, which is exactly what this pipeline must not do quietly.
            entry.reason = "ответ не подтверждён ни одной выноской листа"
            log.append(entry)
            continue
        target[key] = float(value)
        entry.accepted = True
        entry.reason = (
            "подтверждён выноской листа" if allowed else "выноски не прочитаны, принято как есть"
        )
        log.append(entry)

    accepted = [item for item in log if item.accepted]
    if accepted:
        logger.info(
            "cad_spec_followup",
            asked=len(log),
            accepted=len(accepted),
            fields=[item.path for item in accepted],
        )
        _record_followup_provenance(updated, accepted)
    return updated, [item.as_dict() for item in log]


def _record_followup_provenance(spec: dict, accepted: list[FollowupAnswer]) -> None:
    """Mark which values came from a second look, not from the first read.

    Stored on the spec rather than only in the caller's log: the value and the
    story of where it came from must travel together, or the sheet arrives in
    the editor with numbers nobody can rank.
    """
    provenance = spec.setdefault("provenance", {})
    if not isinstance(provenance, dict):  # a reader that wrote junk here
        provenance = {}
        spec["provenance"] = provenance
    for item in accepted:
        provenance[f"{item.path}.{item.field}"] = {
            "origin": "vlm_followup",
            "detail": "прочитано адресным довопросом по фрагменту листа",
            "value_mm": item.answer,
        }
