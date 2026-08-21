"""Matching a catalog entry to the company's canonical item list.

Э6 stage `canonical`. Deliberately conservative: an automatic mapping is only
written when the evidence is strong (an alias/name match, or a high lexical
score). Everything in the middle is recorded as a candidate for review, and
anything weak is left alone — a wrong canonical mapping silently corrupts price
history and consumption statistics, which is far more expensive than no mapping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

AUTO_THRESHOLD = 0.75
REVIEW_THRESHOLD = 0.5


@dataclass
class CanonicalMatch:
    canonical_item_id: str | None
    canonical_name: str | None
    score: float
    decision: str  # "auto" | "review" | "none"


def normalize(text: str | None) -> str:
    if not text:
        return ""
    lowered = text.lower().replace("ё", "е")
    return re.sub(r"[^a-zа-я0-9]+", " ", lowered).strip()


def tokens(text: str | None) -> set[str]:
    return {token for token in normalize(text).split() if len(token) > 1}


def score_pair(entry_text: str, canonical_name: str, aliases: list | None) -> float:
    entry_tokens = tokens(entry_text)
    if not entry_tokens:
        return 0.0
    best = 0.0
    for candidate in [canonical_name, *(aliases or [])]:
        candidate_tokens = tokens(str(candidate))
        if not candidate_tokens:
            continue
        overlap = len(entry_tokens & candidate_tokens)
        if not overlap:
            continue
        # Jaccard-style, but favouring full coverage of the canonical name:
        # "Фреза концевая Ø12 твердосплавная" should match canonical "Фреза
        # концевая" strongly even though the entry carries extra tokens.
        coverage = overlap / len(candidate_tokens)
        jaccard = overlap / len(entry_tokens | candidate_tokens)
        best = max(best, 0.7 * coverage + 0.3 * jaccard)
    return round(min(best, 1.0), 3)


def best_match(
    entry_text: str,
    candidates: list[tuple[str, str, list | None]],
) -> CanonicalMatch:
    """candidates: (canonical_item_id, name, aliases)."""
    best = CanonicalMatch(None, None, 0.0, "none")
    for item_id, name, aliases in candidates:
        score = score_pair(entry_text, name, aliases)
        if score > best.score:
            best = CanonicalMatch(item_id, name, score, "none")
    if best.score >= AUTO_THRESHOLD:
        best.decision = "auto"
    elif best.score >= REVIEW_THRESHOLD:
        best.decision = "review"
    else:
        best = CanonicalMatch(None, None, best.score, "none")
    return best
