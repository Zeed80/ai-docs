"""Read the sheet more than once and keep only what the reads agree on.

A single read is a one-shot bet. Measured live on the same flange: one pass
returned a complete profile, another returned none; on a spindle a bore came
back as Ø18 where the sheet says Ø80H7. The reader is not merely inaccurate,
it is INCONSISTENT — and inconsistency is information we were throwing away.

So the sheet is read N times and the results are intersected. A value that two
independent passes agree on is worth trusting; a value that changes between
passes is exactly the one that must not reach geometry, and it is recorded as
unresolved rather than resolved by majority-of-two coin flip.

This is deliberately CONSERVATIVE. Consensus can only remove values, never add
them, so it cannot invent a part — at worst it declines to build one that a
single lucky pass would have built.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

# Two readings of the same dimension agree within this fraction — the same
# 0.5% window every other check in this pipeline uses.
_NUMERIC_TOLERANCE = 0.005
_NUMERIC_FLOOR = 0.05

# Minimum passes that must agree before a value is kept. With the default of
# three reads this means a strict majority.
MIN_AGREEMENT = 2


def _numbers_agree(left: Any, right: Any) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    window = max(_NUMERIC_FLOOR, abs(float(right)) * _NUMERIC_TOLERANCE)
    return abs(float(left) - float(right)) <= window


def _text_key(value: Any) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def _vote_number(values: list[Any], *, minimum: int) -> tuple[float | None, int]:
    """The numeric value the reads agree on, and how many agreed.

    Agreement is by proximity, not by equality: two passes reading 559.9 and
    560.0 off the same sheet mean the same dimension.
    """
    numbers = [
        float(value) for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    best: tuple[float, int] | None = None
    for candidate in numbers:
        agreeing = [other for other in numbers if _numbers_agree(other, candidate)]
        if best is None or len(agreeing) > best[1]:
            best = (sum(agreeing) / len(agreeing), len(agreeing))
    if best is None or best[1] < minimum:
        return None, (best[1] if best else 0)
    return best[0], best[1]


def _vote_text(values: list[Any], *, minimum: int) -> tuple[str | None, int]:
    keyed = [(_text_key(v), v) for v in values if _text_key(v)]
    if not keyed:
        return None, 0
    counts = Counter(key for key, _ in keyed)
    key, count = counts.most_common(1)[0]
    if count < minimum:
        return None, count
    # Return the original spelling, not the normalised key.
    return next(original for k, original in keyed if k == key), count


def _sections_agree(left: list[dict], right: list[dict]) -> bool:
    if len(left) != len(right):
        return False
    for a, b in zip(left, right, strict=True):
        if not _numbers_agree(a.get("diameter_mm"), b.get("diameter_mm")):
            return False
        if not _numbers_agree(a.get("length_mm"), b.get("length_mm")):
            return False
    return True


def _vote_sections(
    reads: list[list[dict]], *, minimum: int
) -> tuple[list[dict] | None, int, str | None]:
    """A stepped profile is kept only when whole passes agree on ALL of it.

    Merging section-by-section across disagreeing reads would silently build a
    part that no pass actually described — a chimera with one read's diameters
    and another's lengths.
    """
    populated = [read for read in reads if read]
    if not populated:
        # Every pass agreed there is nothing here — a flange has no stepped
        # outer profile at all. Agreement that a thing is absent is agreement,
        # not a disagreement, and reporting it as one put "профиль не прочитан"
        # into unresolved for every correctly-read plate and flange.
        return None, 0, None
    best: tuple[list[dict], int] | None = None
    for candidate in populated:
        agreeing = sum(1 for other in populated if _sections_agree(other, candidate))
        if best is None or agreeing > best[1]:
            best = (candidate, agreeing)
    assert best is not None
    if best[1] < minimum:
        counts = sorted({len(read) for read in populated})
        return None, best[1], (
            "проходы чтения не сошлись на профиле "
            f"(ступеней по проходам: {counts}, совпало {best[1]} из {len(reads)})"
        )
    return best[0], best[1], None


def _body_consensus(
    bodies: list[dict], *, minimum: int, total: int, label: str
) -> tuple[dict, list[str]]:
    """Consensus for one body: its type, its outer profile and its bore."""
    disagreements: list[str] = []
    merged: dict[str, Any] = {}

    body_type, _count = _vote_text([body.get("type") for body in bodies], minimum=minimum)
    merged["type"] = body_type or ""

    outer, _agreed, problem = _vote_sections(
        [body.get("outer") or [] for body in bodies], minimum=minimum
    )
    if outer is not None:
        merged["outer"] = outer
    elif problem:
        disagreements.append(f"{label}: {problem}")

    bore_reads = [body.get("bore") or [] for body in bodies]
    if any(bore_reads):
        bore, _agreed_bore, bore_problem = _vote_sections(bore_reads, minimum=minimum)
        if bore is not None:
            merged["bore"] = bore
        elif bore_problem:
            # A cavity only some passes saw is a review item, not a silent solid.
            disagreements.append(f"{label} (расточка): {bore_problem}")

    profiles = [body.get("profile") for body in bodies if isinstance(body.get("profile"), dict)]
    if profiles:
        profile, profile_problem = _profile_consensus(
            profiles, minimum=minimum, seen=len(profiles), total=total
        )
        if profile is not None:
            merged["profile"] = profile
        elif profile_problem:
            disagreements.append(f"{label} (контур): {profile_problem}")
    return merged, disagreements


def _profile_consensus(
    profiles: list[dict], *, minimum: int, seen: int, total: int
) -> tuple[dict | None, str | None]:
    if seen < minimum:
        return None, (
            f"контур прочитан только в {seen} из {total} проходов"
        )
    merged: dict[str, Any] = {}
    shape, shape_votes = _vote_text([p.get("shape") for p in profiles], minimum=minimum)
    if shape is None:
        return None, "проходы не сошлись на форме контура"
    merged["shape"] = shape
    for field in ("width_mm", "height_mm", "diameter_mm", "thickness_mm"):
        value, _votes = _vote_number([p.get(field) for p in profiles], minimum=minimum)
        merged[field] = value
    # Holes and slots are kept from the passes that agreed on the shape, using
    # the reading with the most features so a pass that simply saw less does
    # not erase them; the count itself is surfaced for review.
    richest = max(profiles, key=lambda p: len(p.get("holes") or []) + len(p.get("slots") or []))
    for field in ("holes", "hole_patterns", "slots"):
        merged[field] = richest.get(field) or []
    return merged, None


def consensus_spec(specs: list[dict], *, minimum: int = MIN_AGREEMENT) -> dict:
    """Intersect several reads of the same sheet into one conservative spec.

    The result carries a ``consensus`` block describing what agreed, and every
    disagreement is appended to ``unresolved`` so the fail-closed contract stops
    construction exactly where the reads stopped agreeing.
    """
    usable = [spec for spec in specs if isinstance(spec, dict) and spec]
    if not usable:
        return {}
    if len(usable) == 1:
        merged = dict(usable[0])
        merged["consensus"] = {
            "passes": 1, "usable": 1, "agreement": "single_pass",
        }
        return merged

    total = len(usable)
    disagreements: list[str] = []
    merged: dict[str, Any] = {"schema_version": 1}

    part_reads = [spec.get("part") for spec in usable]
    part, part_votes = _vote_text(part_reads, minimum=minimum)
    merged["part"] = part or ""
    # Only a real conflict counts: if no pass read a name at all, they agree
    # that the stamp did not give one. A name is metadata anyway — it never
    # blocks geometry, so a genuine conflict is optional, not unresolved.
    if part is None and any(_text_key(value) for value in part_reads):
        optional_conflicts = ["проходы не сошлись на названии детали"]
    else:
        optional_conflicts = []

    main_bodies = [spec.get("main_view") or {} for spec in usable]
    main, main_problems = _body_consensus(
        main_bodies, minimum=minimum, total=total, label="главный вид"
    )
    merged["main_view"] = main
    disagreements.extend(main_problems)

    # Title-block metadata never blocks geometry, so a disagreement here is
    # optional rather than fatal.
    optional: list[str] = list(optional_conflicts)
    title: dict[str, Any] = {}
    title_reads = [spec.get("title_block") or {} for spec in usable]
    for field in ("material", "designation", "scale", "company", "mass"):
        value, votes = _vote_text([t.get(field) for t in title_reads], minimum=minimum)
        if value is not None:
            title[field] = value
        elif any(t.get(field) for t in title_reads):
            optional.append(f"штамп: поле «{field}» различается между проходами")
    merged["title_block"] = title

    # A callout confirmed by one pass only is not confirmed.
    merged["dimensions"] = _agreed_items(
        [spec.get("dimensions") or [] for spec in usable], "value", minimum=minimum
    )
    merged["annotations"] = _agreed_items(
        [spec.get("annotations") or [] for spec in usable], "text", minimum=minimum
    )
    merged["views"] = _agreed_items(
        [spec.get("views") or [] for spec in usable], "kind", minimum=minimum
    )
    merged["parts"] = []

    # Anything a single pass declared unresolved stays unresolved: one reader
    # admitting it could not prove a value is enough to keep it out.
    inherited = sorted({
        str(item) for spec in usable for item in (spec.get("unresolved") or []) if str(item)
    })
    merged["unresolved"] = sorted(set(disagreements) | set(inherited))
    merged["optional_unresolved"] = sorted(set(optional) | {
        str(item) for spec in usable
        for item in (spec.get("optional_unresolved") or []) if str(item)
    })
    merged["consensus"] = {
        "passes": len(specs),
        "usable": total,
        "minimum_agreement": minimum,
        "part_votes": part_votes,
        "disagreements": disagreements,
    }
    return merged


def _agreed_items(reads: list[list[dict]], key: str, *, minimum: int) -> list[dict]:
    """Keep list items whose key text appears in at least ``minimum`` passes."""
    counts: Counter[str] = Counter()
    first: dict[str, dict] = {}
    for read in reads:
        seen_here: set[str] = set()
        for item in read:
            if not isinstance(item, dict):
                continue
            text = _text_key(item.get(key))
            if not text or text in seen_here:
                continue
            seen_here.add(text)
            counts[text] += 1
            first.setdefault(text, item)
    return [first[text] for text, count in counts.items() if count >= minimum]


def consensus_summary(spec: dict) -> str:
    """One line for logs and for the review card."""
    block = spec.get("consensus") or {}
    return json.dumps(
        {
            "passes": block.get("passes"),
            "usable": block.get("usable"),
            "disagreements": len(block.get("disagreements") or []),
        },
        ensure_ascii=False,
    )
