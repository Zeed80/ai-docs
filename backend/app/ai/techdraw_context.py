"""Deterministic (regex/keyword, no extra LLM call) grounding for techdraw specs.

Before the LLM turns a free-text part description into a ``techdraw`` JSON
spec, scan the description for material/thread/tolerance mentions and look
them up in ``techdraw_reference`` — then hand the LLM the exact values instead
of letting it "pick something realistic". This is intentionally NOT a RAG
retrieval (no embeddings/vector search): the reference data is small and
structured, so a plain keyword/regex lookup is enough and stays deterministic.
"""

from __future__ import annotations

import re

from app.ai import techdraw_reference as tdref

_THREAD_RE = re.compile(r"\bM\s*(\d+(?:[.,]\d+)?)(?:\s*[×xXхХ]\s*(\d+(?:[.,]\d+)?))?", re.IGNORECASE)
_DIM_RE = re.compile(r"(?:ø|⌀|d\s*=?\s*)?(\d+(?:[.,]\d+)?)\s*(?:мм)?", re.IGNORECASE)
_TOLERANCE_MARKERS = ("допуск", "посадк", "квалитет", "h6", "h7", "h8", "h9", "js6", "k6", "m6", "n6", "p6")


def _material_hint(description: str) -> str | None:
    spec = tdref.classify_material(description)
    if not spec:
        return None
    return f"Материал по тексту запроса: {spec.designation} (группа {spec.group})."


def _thread_hints(description: str) -> list[str]:
    hints: list[str] = []
    seen: set[float] = set()
    for m in _THREAD_RE.finditer(description):
        d = float(m.group(1).replace(",", "."))
        if d in seen:
            continue
        seen.add(d)
        base = tdref.METRIC_THREAD_TABLE.get(d)
        if not base:
            continue
        fine = ", ".join(f"{p:g}" for p in base.fine_pitches_mm) or "нет"
        hints.append(
            f"Резьба M{d:g}: крупный шаг {base.coarse_pitch_mm:g}мм, мелкие шаги: {fine}."
        )
    return hints


def _tolerance_hint(description: str) -> str | None:
    low = description.lower()
    if not any(kw in low for kw in _TOLERANCE_MARKERS):
        return None
    for m in _DIM_RE.finditer(description):
        raw = m.group(1)
        if not raw:
            continue
        d = float(raw.replace(",", "."))
        if not (1 <= d <= 400):
            continue
        band = tdref.tolerance_band("h7", d)
        if band:
            return (
                f"Справочно (пример квалитета h7 для Ø{d:g}мм): "
                f"допуск {band.ei_um:g}…{band.es_um:g} мкм — используй ТОЧНО такие "
                "значения по таблице для запрошенного квалитета/посадки, не выдумывай."
            )
    return None


def build_context_block(description: str) -> str:
    """Return a ready-to-append system-prompt text block, or ``""`` if nothing found."""
    if not description or not description.strip():
        return ""
    parts: list[str] = []
    if material := _material_hint(description):
        parts.append(material)
    parts.extend(_thread_hints(description))
    if tol := _tolerance_hint(description):
        parts.append(tol)
    return "\n".join(parts)
