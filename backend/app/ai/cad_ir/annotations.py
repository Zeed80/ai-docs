"""Canonical text and validation helpers for structured ЕСКД annotations (C4).

One place that knows how each annotation ``kind`` reads on the sheet and what
makes it valid, so the renderers (PNG/SVG/DXF) and the validator agree.
"""

from __future__ import annotations

# ГОСТ 2.308 geometric-tolerance symbols → their conventional glyph. Kept as
# text (not the Unicode GD&T glyphs, which few fonts carry) so it survives
# every render target and DXF consumer.
TOLERANCE_SYMBOLS: dict[str, str] = {
    "straightness": "—",       # прямолинейность
    "flatness": "▱",           # плоскостность
    "roundness": "○",          # круглость
    "cylindricity": "⌭",       # цилиндричность
    "profile_line": "⌒",       # профиль продольного сечения
    "parallelism": "∥",        # параллельность
    "perpendicularity": "⊥",   # перпендикулярность
    "angularity": "∠",         # наклон
    "position": "⊕",           # позиционный допуск
    "concentricity": "◎",      # соосность
    "symmetry": "⌯",           # симметричность
    "runout": "↗",             # биение
}

# ГОСТ 2.312 weld types (a small, common subset).
WELD_TYPES = frozenset({"С", "У", "Т", "Н", "fillet", "butt", "spot"})


def annotation_text(
    kind: str,
    value: str | None = None,
    symbol: str | None = None,
    datum_refs: list[str] | None = None,
) -> str:
    """The canonical display string for an annotation, e.g. "Ra 3,2",
    "M20×1.5", "⊥ 0.05 A"."""
    datum_refs = datum_refs or []
    if kind == "roughness":
        v = (value or "").strip()
        if not v:
            return "Ra"
        return v if v.lower().startswith(("ra", "rz")) else f"Ra {v}"
    if kind == "thread":
        return (value or "").strip()
    if kind == "tolerance":
        glyph = TOLERANCE_SYMBOLS.get((symbol or "").strip(), symbol or "?")
        parts = [glyph]
        if value:
            parts.append(str(value).strip())
        parts.extend(datum_refs)
        return " ".join(p for p in parts if p)
    if kind == "datum":
        return (symbol or value or "").strip() or "A"
    if kind == "weld":
        return (value or symbol or "").strip()
    return (value or "").strip()


def validate_annotation(entity) -> tuple[bool, str | None]:
    """(ok, message_ru | None) — deterministic ЕСКД check for one annotation.

    Kept side-effect-free; ``cad_validate`` wraps the failures into
    profile-backed issues."""
    from app.ai import techdraw_reference as tdref

    kind = entity.kind
    if kind == "roughness":
        raw = (entity.value or "").strip().lower().removeprefix("ra").removeprefix("rz").strip()
        try:
            v = float(raw.replace(",", "."))
        except (ValueError, AttributeError):
            return True, None  # unreadable value stays for review, not an error
        nearest = tdref.nearest_ra(v)
        if abs(nearest - v) > 1e-6:
            return False, f"Ra {v:g} не из ряда ГОСТ 2789 (ближайшее — Ra {nearest:g})"
        return True, None
    if kind == "thread":
        if entity.value and tdref.parse_thread(entity.value) is None:
            return False, f"Резьба «{entity.value}» не распознана (ГОСТ 8724)"
        return True, None
    if kind == "tolerance":
        if entity.symbol and entity.symbol not in TOLERANCE_SYMBOLS:
            return False, f"Неизвестный символ допуска формы/расположения: {entity.symbol}"
        return True, None
    if kind == "datum":
        letter = (entity.symbol or entity.value or "").strip()
        if len(letter) != 1 or not letter.isalpha():
            return False, "База обозначается одной буквой (ГОСТ 2.308)"
        return True, None
    return True, None
