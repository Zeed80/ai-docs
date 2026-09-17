"""Сечение на листе ↔ паз ↔ вид спека: межвидовое соответствие вала (план, Ф5).

Допуск графа требует хотя бы одного ребра `same_object_across_views`, а оно
строится, когда элемент показан на двух видах (``features_shown``). У живого
z4-r4 паз подтверждён на главном виде, сечения А-А и Б-Б найдены на листе
кругами (`section_disk`), но какой круг — какой вид спека и через какой паз он
проведён, связано не было, и угадывать это нельзя.

Два независимых шага:

* **круг → паз** — по геометрии: Ø круга однозначно указывает ступень с пазом
  (ближайшая среди ступеней с пазами в допуске круга, вторая — заметно
  дальше), и на этой ступени ровно один паз;
* **круг → вид спека** — по надписи: обозначение над кругом читает модель по
  вырезу вокруг круга («надписи чтением»), не видя, что ожидается. Ответ
  принимается, только если совпал с обозначением ровно одного вида-сечения
  спека и этот вид не занят другим кругом.

Не прошёл любой шаг — связи нет, паз остаётся на одном виде.
"""

from __future__ import annotations

import io
import re
from collections.abc import Awaitable, Callable
from typing import Any

# Ø круга отвечает Ø ступени с этим допуском (как у `section_disk`); соседняя
# ступень с пазом должна быть дальше вдвое — иначе круг не различает ступени.
_DIAMETER_SHARE = 0.06
_SECTION_KINDS = {"section", "removed_section", "cut", "разрез", "сечение"}

_PROMPT = (
    "Перед тобой фрагмент чертежа. В центре — сечение детали: круг со штриховкой.\n"
    "Какое ОБОЗНАЧЕНИЕ этого сечения написано рядом с ним (обычно над ним), "
    "например «А-А» или «Б-Б»? Не выдумывай: если обозначения на фрагменте не "
    "видно, верни null.\n"
    'Ответь ОДНОЙ строкой JSON: {"label": "А-А"} или {"label": null}'
)
_SCHEMA = {"type": "object", "properties": {"label": {"type": ["string", "null"]}}}

Asker = Callable[[str, Any], Awaitable[dict]]


def _keyway_steps(spec: dict[str, Any]) -> list[tuple[dict[str, Any], float]]:
    body = spec.get("main_view") or {}
    outer = [s for s in body.get("outer") or [] if isinstance(s, dict)]
    result = []
    for keyway in body.get("keyways") or []:
        if not isinstance(keyway, dict) or not keyway.get("id"):
            continue
        start, length = keyway.get("axial_start_mm"), keyway.get("length_mm")
        if not isinstance(start, (int, float)) or not isinstance(length, (int, float)):
            continue
        middle, position = float(start) + float(length) / 2.0, 0.0
        for step in outer:
            step_length = float(step.get("length_mm") or 0.0)
            if position <= middle <= position + step_length and step.get("diameter_mm"):
                result.append((keyway, float(step["diameter_mm"])))
                break
            position += step_length
    return result


def link_disks_to_keyways(
    spec: dict[str, Any], disks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Круг сечения → паз по Ø ступени; неоднозначное не связывается."""
    keyed = _keyway_steps(spec)
    links: list[dict[str, Any]] = []
    taken: set[str] = set()
    for index, disk in enumerate(disks):
        if not disk.get("solid") or not disk.get("diameter_mm"):
            continue
        diameter = float(disk["diameter_mm"])
        by_distance = sorted(keyed, key=lambda item: abs(item[1] - diameter))
        if not by_distance:
            continue
        keyway, step = by_distance[0]
        if abs(step - diameter) > _DIAMETER_SHARE * step:
            continue
        rivals = [item for item in by_distance[1:] if item[1] != step]
        if rivals:
            rival = abs(rivals[0][1] - diameter)
            if rival <= _DIAMETER_SHARE * rivals[0][1] or rival <= 2.0 * abs(step - diameter):
                continue  # круг не различает ступени с пазами
        if sum(1 for _key, other in keyed if other == step) != 1:
            continue  # два паза на одной ступени — какой из них, по кругу не понять
        if str(keyway["id"]) in taken:
            continue
        taken.add(str(keyway["id"]))
        links.append(
            {
                "disk_index": index,
                "center_px": disk["center_px"],
                "radius_px": disk.get("radius_px"),
                "diameter_mm": diameter,
                "keyway_id": str(keyway["id"]),
                "step_diameter_mm": step,
            }
        )
    return links


# Латиница, похожая на кириллицу, — та же буква обозначения.
_LOOKALIKE = str.maketrans("ABEKMHOPCTX", "АВЕКМНОРСТХ")


def _letters(label: Any) -> str:
    """«Б–Б», «б-б (2:1)», «Сечение Б-Б» → «Б-Б»; иначе пусто."""
    match = re.search(r"([A-Za-zА-ЯЁа-яё])\s*[-–—]\s*([A-Za-zА-ЯЁа-яё])", str(label or ""))
    if not match:
        return ""
    first, second = (match.group(k).upper().translate(_LOOKALIKE) for k in (1, 2))
    return f"{first}-{second}" if first == second else ""


def label_crop_box(link: dict[str, Any], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """Вырез вокруг круга с запасом сверху — там обозначение (ГОСТ 2.305)."""
    width, height = size
    cx, cy = (float(v) for v in link["center_px"])
    r = float(link.get("radius_px") or 0.03 * min(width, height))
    above = max(3.0 * r, 0.06 * height)
    return (
        int(max(0, cx - 1.8 * r)),
        int(max(0, cy - r - above)),
        int(min(width, cx + 1.8 * r)),
        int(min(height, cy + 1.3 * r)),
    )


async def label_links(
    image_bytes: bytes,
    spec: dict[str, Any],
    links: list[dict[str, Any]],
    *,
    ask: Asker | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Обозначение над каждым кругом — модель по вырезу; вид — по совпадению."""
    from PIL import Image

    if not links:
        return links, []
    sections: dict[str, list[dict[str, Any]]] = {}
    for view in spec.get("views") or []:
        if isinstance(view, dict) and str(view.get("kind") or "").lower() in _SECTION_KINDS:
            key = _letters(view.get("label"))
            if key:
                sections.setdefault(key, []).append(view)
    if not sections:
        return links, []
    if ask is None:
        ask = _default_ask
    try:
        sheet = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:  # noqa: BLE001 — нет картинки — нет связи
        return links, []
    taken: set[str] = set()
    log: list[dict[str, Any]] = []
    labelled = []
    for link in links:
        crop = sheet.crop(label_crop_box(link, sheet.size))
        answer = await ask(_PROMPT, crop)
        read = answer.get("label") if isinstance(answer, dict) else None
        key = _letters(read)
        entry = {"keyway_id": link["keyway_id"], "answer": read}
        views = sections.get(key) or []
        if not key:
            entry["outcome"] = "обозначение не прочитано"
        elif len(views) != 1:
            entry["outcome"] = f"в спеке видов «{key}»: {len(views)}"
        elif str(views[0].get("view_id")) in taken:
            entry["outcome"] = f"вид «{key}» уже связан с другим кругом"
        else:
            view_id = str(views[0].get("view_id"))
            taken.add(view_id)
            link = {**link, "view_id": view_id, "label": key}
            entry["outcome"] = f"связано: {key}"
        log.append(entry)
        labelled.append(link)
    return labelled, log


def attach_section_views(spec: dict[str, Any], links: list[dict[str, Any]]) -> dict[str, Any]:
    """Паз, показанный на главном виде, — и на своём сечении (features_shown)."""
    import copy

    linked = [link for link in links if link.get("view_id")]
    if not linked:
        return spec
    spec = copy.deepcopy(spec)
    views = [v for v in spec.get("views") or [] if isinstance(v, dict)]
    by_id = {str(v.get("view_id")): v for v in views}
    for link in linked:
        view = by_id.get(str(link["view_id"]))
        shown_elsewhere = any(
            link["keyway_id"] in (v.get("features_shown") or []) for v in views if v is not view
        )
        if view is None or not shown_elsewhere:
            continue
        view["features_shown"] = list(
            dict.fromkeys([*(view.get("features_shown") or []), link["keyway_id"]])
        )
        cx, cy = (float(v) for v in link["center_px"])
        r = float(link.get("radius_px") or 0.0)
        view.setdefault("evidence", []).append(
            {
                "image_index": 0,
                "bbox": [cx - r, cy - r, cx + r, cy + r],
                "raw_text": (
                    f"сечение {link['label']} найдено на листе: круг Ø{link['diameter_mm']:g}"
                    f" на ступени Ø{link['step_diameter_mm']:g} с пазом"
                ),
            }
        )
    return spec


async def _default_ask(prompt: str, crop: Any) -> dict:
    from app.ai.cad_recognize.spec_fragments import _ask, _overview
    from app.ai.router import ai_router

    return await _ask(
        prompt,
        _overview(crop),
        router=ai_router,
        confidential=True,
        num_predict=120,
        schema=_SCHEMA,
    )
