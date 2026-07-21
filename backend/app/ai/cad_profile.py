"""Deterministic drawing-domain profile selection for vectorization routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProfileDecision:
    profile: str
    confidence: float
    evidence: tuple[str, ...]


_MECHANICAL = (
    re.compile(r"(?:^|\W)[mм]\s*\d+(?:[.,]\d+)?", re.IGNORECASE),
    re.compile(r"[ø⌀]\s*\d+", re.IGNORECASE),
    re.compile(r"\bra\s*\d", re.IGNORECASE),
    re.compile(r"\b(?:h7|h7|js\d+|g6)\b", re.IGNORECASE),
    re.compile(r"\b(?:деталь|материал|масса|литера)\b", re.IGNORECASE),
)
_CONSTRUCTION = (
    re.compile(r"\b(?:план|фасад|разрез|экспликация)\b", re.IGNORECASE),
    re.compile(r"\b(?:этаж|ось|оси|стена|перекрытие)\b", re.IGNORECASE),
    re.compile(r"\b(?:м\s*1\s*:\s*(?:50|100|200|500))\b", re.IGNORECASE),
)
_CONSTRUCTION_FILENAME = re.compile(
    r"\b(?:план|фасад|разрез|перекрыти|этаж|floor|elevation|section)\w*",
    re.IGNORECASE,
)
_MECHANICAL_FILENAME = re.compile(
    r"\b(?:деталь|сборочн|вал|шестерн|корпус|втулк|кронштейн|part|assembly|shaft|gear)\w*",
    re.IGNORECASE,
)


def choose_profile(
    requested: str | None,
    texts: list[str],
    source_filename: str | None = None,
) -> ProfileDecision:
    """Resolve ``auto`` to a domain without pretending weak evidence is fact."""
    normalized = (requested or "auto").strip().lower()
    if normalized in (
        "mechanical",
        "mechanical_eskd",
        "construction",
        "electrical",
        "hydraulic",
        "pid",
    ):
        profile = "mechanical" if normalized == "mechanical_eskd" else normalized
        return ProfileDecision(profile, 1.0, ("user_selected",))

    filename = Path(source_filename or "").stem.replace("_", " ")
    corpus = " ".join([filename, *texts])
    if _CONSTRUCTION_FILENAME.search(filename):
        return ProfileDecision(
            "construction",
            0.85,
            ("construction_filename",),
        )
    if _MECHANICAL_FILENAME.search(filename):
        return ProfileDecision(
            "mechanical",
            0.85,
            ("mechanical_filename",),
        )
    mechanical = [pattern.pattern for pattern in _MECHANICAL if pattern.search(corpus)]
    construction = [pattern.pattern for pattern in _CONSTRUCTION if pattern.search(corpus)]
    if len(mechanical) >= len(construction) + 2:
        return ProfileDecision(
            "mechanical",
            min(0.95, 0.55 + 0.1 * len(mechanical)),
            tuple(mechanical),
        )
    if len(construction) >= len(mechanical) + 2:
        return ProfileDecision(
            "construction",
            min(0.95, 0.55 + 0.1 * len(construction)),
            tuple(construction),
        )
    return ProfileDecision("auto", 0.0, tuple(mechanical + construction))
