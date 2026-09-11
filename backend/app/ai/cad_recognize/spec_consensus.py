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

# Minimum passes that must agree before a value is kept. Kept as the floor and
# as the explicit override tests pass in; the effective threshold now scales
# with how many passes actually came back usable — see required_agreement.
MIN_AGREEMENT = 2


def required_agreement(usable: int) -> int:
    """Strict majority of the passes that actually returned something.

    A fixed 2 was wrong in both directions at once. The pipeline runs five
    passes by default and the UI offers up to five, so 2 of 5 is not a majority
    — it is a pair agreeing while three others say something else. And when
    only two passes survive validation, the same 2 demands unanimity, which is
    how a live shaft read correctly by both surviving passes produced no
    profile at all: they differed on one step out of six.

    Worse, the two failures compose into a perverse rule. A single usable pass
    is passed through whole and unchecked (see ``consensus_spec``), so two
    slightly-disagreeing reads used to yield strictly LESS than one read —
    more evidence produced a worse answer, and asking for more passes made
    success less likely while costing five times the time.

    1 -> 1, 2 -> 2, 3 -> 2, 4 -> 3, 5 -> 3.
    """
    return 1 if usable <= 1 else max(2, usable // 2 + 1)


_PROVENANCE_SKIP = {
    "consensus",
    "reader_attempts",
    "reader_raw_response",
    "source_images",
    "unresolved",
    "optional_unresolved",
    "value_provenance",
    "evidence",
}


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
        float(value)
        for value in values
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

    def same_optional_number(a: Any, b: Any) -> bool:
        # Two passes that both leave a value unread are in agreement about the
        # partial observation. Keep the observed diameters in the audit spec;
        # the full validator still records every missing length as unresolved,
        # so this can never promote incomplete geometry to the CAD kernel.
        if a is None and b is None:
            return True
        return _numbers_agree(a, b)

    for a, b in zip(left, right, strict=True):
        if not same_optional_number(a.get("diameter_mm"), b.get("diameter_mm")):
            return False
        if not same_optional_number(a.get("length_mm"), b.get("length_mm")):
            return False
    return True


def _disputed_values(reads: list[list[dict]], index: int, field: str) -> list[float]:
    """Distinct values the passes gave for one field of one step."""
    seen: list[float] = []
    for read in reads:
        if index >= len(read):
            continue
        value = read[index].get(field)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if not any(_numbers_agree(value, known) for known in seen):
            seen.append(float(value))
    return seen


def _mark_disputed_sections(
    accepted: list[dict], populated: list[list[dict]], label: str
) -> list[str]:
    """Flag the steps the passes disagreed on; return the review notes.

    The whole profile is kept — a part the operator can look at and correct
    beats no part at all — but every step that is not backed by agreement says
    so, in the spec and in ``unresolved``, so it can never pass silently into
    geometry.
    """
    notes: list[str] = []
    for index, section in enumerate(accepted):
        disputed: list[str] = []
        for field, caption in (("diameter_mm", "Ø"), ("length_mm", "L")):
            values = _disputed_values(populated, index, field)
            if len(values) > 1:
                disputed.append(f"{caption}: {', '.join(f'{v:g}' for v in values)}")
        if not disputed:
            continue
        section["review_required"] = True
        section["disputed_values"] = _disputed_values(populated, index, "diameter_mm")
        notes.append(f"{label}: ступень {index + 1} не подтверждена ({'; '.join(disputed)})")
    return notes


def _vote_sections(
    reads: list[list[dict]], *, minimum: int, label: str = "профиль"
) -> tuple[list[dict] | None, int, str | None]:
    """Keep the best-supported stepped profile, marking what did not agree.

    Merging section-by-section across disagreeing reads would silently build a
    part no pass described — a chimera with one read's diameters and another's
    lengths — so the accepted profile is still ONE pass's list, taken whole.
    That part has not changed.

    What changed is the else-branch. Returning ``None`` when no list won a
    majority threw away every step, including the ones every pass agreed on,
    and the caller then reported "the drawing has no stepped profile" — which
    on a live A3 shaft became "you picked the wrong part type" while the reader
    had in fact read the shaft correctly twice. Now the best-supported list
    survives with its unconfirmed steps flagged ``review_required``, and the
    disagreement is still recorded so nothing unverified reaches the kernel.
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
    accepted = [dict(item) for item in best[0]]
    agreeing_reads = [read for read in populated if _sections_agree(read, best[0])]
    # Threads are voted the same way in both outcomes: a thread is an annotation
    # on a step, and it needs its own agreement whether or not the profile as a
    # whole won one. Below the threshold that vote simply has fewer voters.
    for index, section in enumerate(accepted):
        thread_reads = [
            [read[index]["thread"]] if isinstance(read[index].get("thread"), dict) else []
            for read in agreeing_reads
        ]
        agreed_threads = _agreed_feature_items(
            thread_reads, minimum=min(minimum, len(agreeing_reads))
        )
        if agreed_threads:
            section["thread"] = agreed_threads[0]
        else:
            section.pop("thread", None)

    if best[1] >= minimum:
        return accepted, best[1], None

    # "The passes disagree about WHAT is here" and "most passes say nothing is
    # here" are different findings and must not share an outcome. Keeping the
    # best-supported list is right for the first: every pass saw a stepped
    # profile and they differed on a step, so the operator gets the part with
    # that step flagged. It would be wrong for the second: a bore only one pass
    # out of three ever saw is a cavity two passes deny, and adding it marked
    # still adds it. Below a majority of passes populating the field at all, the
    # old fail-closed answer stands.
    if len(populated) < minimum:
        counts = sorted({len(read) for read in populated})
        return (
            None,
            best[1],
            (
                "элемент увидели не все проходы "
                f"(увидели {len(populated)} из {len(reads)}, ступеней: {counts})"
            ),
        )

    counts = sorted({len(read) for read in populated})
    disputed_notes = _mark_disputed_sections(accepted, populated, label)
    problem = (
        "проходы чтения не сошлись на профиле "
        f"(ступеней по проходам: {counts}, совпало {best[1]} из {len(reads)}); "
        "профиль сохранён под ревью"
    )
    if disputed_notes:
        problem = f"{problem}. {'. '.join(disputed_notes)}"
    return accepted, best[1], problem


def _body_consensus(
    bodies: list[dict], *, minimum: int, total: int, label: str
) -> tuple[dict, list[str], list[str]]:
    """Consensus for one body: its type, its outer profile and its bore.

    Returns ``(body, disagreements, notes)`` — disagreements block
    construction, notes record features dropped as unconfirmed.
    """
    disagreements: list[str] = []
    notes: list[str] = []
    merged: dict[str, Any] = {}

    body_type, _count = _vote_text([body.get("type") for body in bodies], minimum=minimum)
    merged["type"] = body_type or ""
    name, _count = _vote_text([body.get("name") for body in bodies], minimum=minimum)
    if name is not None:
        merged["name"] = name

    outer, _agreed, problem = _vote_sections(
        [body.get("outer") or [] for body in bodies], minimum=minimum, label=label
    )
    # `problem` is recorded whether or not sections survived: the profile can
    # now come back marked-for-review rather than absent, and an `elif` here
    # would drop exactly the note that says so. Keeping the geometry must never
    # cost the record of why it is not confirmed.
    if outer is not None:
        merged["outer"] = outer
    if problem:
        disagreements.append(f"{label}: {problem}")

    bore_reads = [body.get("bore") or [] for body in bodies]
    if any(bore_reads):
        bore, _agreed_bore, bore_problem = _vote_sections(
            bore_reads, minimum=minimum, label=f"{label} (расточка)"
        )
        if bore is not None:
            merged["bore"] = bore
        if bore_problem:
            # A cavity only some passes saw is a review item, not a silent solid.
            disagreements.append(f"{label} (расточка): {bore_problem}")
        # Where the bore starts and whether it is blind. These were dropped
        # here, so every bore left consensus as a through hole from the left
        # face — the very defect the fields were added to fix.
        if bore is not None:
            placement = _bore_placement(bodies, minimum=minimum)
            if placement is None:
                disagreements.append(f"{label} (расточка): проходы не сошлись на её положении")
            else:
                merged.update(placement)

    profiles = [body.get("profile") for body in bodies if isinstance(body.get("profile"), dict)]
    if profiles:
        profile, profile_problem, profile_notes = _profile_consensus(
            profiles, minimum=minimum, seen=len(profiles), total=total
        )
        if profile is not None:
            merged["profile"] = profile
        elif profile_problem:
            disagreements.append(f"{label} (контур): {profile_problem}")
        notes.extend(f"{label}: {note}" for note in profile_notes)

    for field in (
        "chamfers",
        "fillets",
        "grooves",
        "keyways",
        "cross_holes",
        "axial_holes",
        "circular_hole_patterns",
    ):
        feature_reads = [body.get(field) or [] for body in bodies]
        accepted, dropped = _vote_feature_list(feature_reads, minimum=minimum)
        if accepted:
            merged[field] = accepted
            if dropped:
                notes.append(
                    f"{label} ({field}): {dropped} элемент(ов) подтверждено меньшинством "
                    "проходов — не построено"
                )
        elif any(feature_reads):
            disagreements.append(f"{label} ({field}): проходы не сошлись на малых элементах")
    return merged, disagreements, notes


def _bore_placement(bodies: list[dict], *, minimum: int) -> dict[str, Any] | None:
    """Voted bore start, side and blindness; ``None`` when the passes disagree.

    A field no pass stated takes the schema default — agreement that nothing
    was said is agreement.
    """
    with_bore = [body for body in bodies if body.get("bore")]
    need = min(minimum, len(with_bore))
    placement: dict[str, Any] = {}
    starts = [body.get("bore_start_mm", 0.0) or 0.0 for body in with_bore]
    start, _votes = _vote_number(starts, minimum=need)
    if start is None:
        return None
    placement["bore_start_mm"] = start
    side, _votes = _vote_text(
        [body.get("bore_from_end") or "left" for body in with_bore], minimum=need
    )
    if side is None:
        return None
    placement["bore_from_end"] = side
    blind = [body.get("bore_blind") for body in with_bore]
    counts = Counter(json.dumps(value) for value in blind)
    key, votes = counts.most_common(1)[0]
    if votes < need:
        return None
    placement["bore_blind"] = json.loads(key)
    return placement


def _feature_items_agree(left: dict, right: dict) -> bool:
    """Compare one cut feature without relying on list order or evidence."""
    keys = set(left) | set(right)
    # `id` is a label the reader assigns per pass ("hole-2"), not geometry.
    keys -= {"evidence", "note", "confidence", "source", "id"}
    for key in keys:
        a, b = left.get(key), right.get(key)
        if a is None and b is None:
            continue
        if isinstance(a, bool) or isinstance(b, bool):
            if a is not b:
                return False
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if not _numbers_agree(a, b):
                return False
        elif isinstance(a, dict) and isinstance(b, dict):
            if not _feature_items_agree(a, b):
                return False
        elif _text_key(a) != _text_key(b):
            return False
    return True


def _agreed_feature_items(reads: list[list[dict]], *, minimum: int) -> list[dict]:
    """Keep only complete feature objects independently confirmed by passes."""
    return _vote_feature_list(reads, minimum=minimum)[0]


def _vote_feature_list(reads: list[list[dict]], *, minimum: int) -> tuple[list[dict], int]:
    """Confirmed feature objects, and how many DISTINCT features were dropped.

    The count is what keeps a rejection from being a silent loss: a feature
    only a minority of passes saw is not built, but the spec says so.
    """
    candidates = [item for read in reads for item in read if isinstance(item, dict)]
    accepted: list[dict] = []
    rejected: list[dict] = []
    for candidate in candidates:
        if any(_feature_items_agree(candidate, item) for item in accepted + rejected):
            continue
        votes = sum(
            1
            for read in reads
            if any(
                isinstance(item, dict) and _feature_items_agree(candidate, item) for item in read
            )
        )
        (accepted if votes >= minimum else rejected).append(candidate)
    return accepted, len(rejected)


def _same_value(left: Any, right: Any) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return _numbers_agree(left, right)
    if isinstance(left, str) and isinstance(right, str):
        return _text_key(left) == _text_key(right)
    return left == right


def _iter_leaves(value: Any, path: tuple[Any, ...] = ()):  # noqa: ANN202
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _PROVENANCE_SKIP:
                continue
            yield from _iter_leaves(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_leaves(child, (*path, index))
    elif value is not None and not isinstance(value, (dict, list)):
        yield path, value


def _item_identity(path: tuple[Any, ...], item: dict) -> tuple[str, str] | None:
    root = path[0] if path else None
    key = {"dimensions": "value", "annotations": "text", "views": "kind"}.get(root)
    if key and _text_key(item.get(key)):
        return key, _text_key(item.get(key))
    return None


def _lookup(read: dict, path: tuple[Any, ...], merged: dict) -> tuple[Any, dict | None]:
    """Resolve a merged leaf in one raw pass and return its owning item."""
    current: Any = read
    owner: dict | None = None
    for depth, part in enumerate(path):
        if isinstance(part, int):
            if not isinstance(current, list):
                return None, None
            merged_parent: Any = merged
            for segment in path[:depth]:
                merged_parent = merged_parent[segment]
            merged_item = merged_parent[part] if part < len(merged_parent) else None
            identity = _item_identity(path, merged_item) if isinstance(merged_item, dict) else None
            if identity:
                key, expected = identity
                current = next(
                    (
                        item
                        for item in current
                        if isinstance(item, dict) and _text_key(item.get(key)) == expected
                    ),
                    None,
                )
                if current is None:
                    return None, None
            elif part < len(current):
                current = current[part]
            else:
                return None, None
        elif isinstance(current, dict) and part in current:
            owner = current
            current = current[part]
        else:
            return None, None
    return current, owner


def _source_bbox(evidence: dict, source_images: list[dict]) -> list[float] | None:
    bbox = evidence.get("bbox")
    index = evidence.get("image_index")
    if not isinstance(index, int) or not isinstance(bbox, list) or len(bbox) != 4:
        return None
    image = next((item for item in source_images if item.get("image_index") == index), None)
    if not image:
        return None
    source = image.get("source_bbox")
    width, height = image.get("image_width"), image.get("image_height")
    if not isinstance(source, list) or len(source) != 4 or not width or not height:
        return None
    sx = (float(source[2]) - float(source[0])) / float(width)
    sy = (float(source[3]) - float(source[1])) / float(height)
    return [
        float(source[0]) + float(bbox[0]) * sx,
        float(source[1]) + float(bbox[1]) * sy,
        float(source[0]) + float(bbox[2]) * sx,
        float(source[1]) + float(bbox[3]) * sy,
    ]


def _build_value_provenance(merged: dict, reads: list[dict]) -> dict[str, dict]:
    """Explain every accepted scalar: votes, confidence, raw values and source."""
    result: dict[str, dict] = {}
    total = len(reads)
    for path, accepted in _iter_leaves(merged):
        observations: list[dict] = []
        for pass_index, read in enumerate(reads, start=1):
            observed, owner = _lookup(read, path, merged)
            if observed is None:
                continue
            evidence: list[dict] = []
            if isinstance(owner, dict):
                for raw in owner.get("evidence") or []:
                    if not isinstance(raw, dict):
                        continue
                    item = dict(raw)
                    source = _source_bbox(item, read.get("source_images") or [])
                    if source is not None:
                        item["source_bbox"] = [round(value, 3) for value in source]
                    evidence.append(item)
            observations.append({"pass": pass_index, "value": observed, "evidence": evidence})
        agreeing = [item for item in observations if _same_value(item["value"], accepted)]
        key = "/".join(str(part) for part in path)
        result[key] = {
            "value": accepted,
            "votes": len(agreeing),
            "passes": total,
            "confidence": round(len(agreeing) / total, 3) if total else 0.0,
            "accepted_from_passes": [item["pass"] for item in agreeing],
            "observations": [
                {"pass": item["pass"], "value": item["value"]} for item in observations
            ],
            "evidence": [
                {**evidence, "pass": item["pass"]}
                for item in agreeing
                for evidence in item["evidence"]
            ],
        }
    return result


def _profile_consensus(
    profiles: list[dict], *, minimum: int, seen: int, total: int
) -> tuple[dict | None, str | None, list[str]]:
    """Consensus for a flat profile: shape, sizes, sketch and cut features.

    Returns ``(profile, problem, notes)``: ``problem`` blocks construction,
    ``notes`` are features dropped as unconfirmed while others agreed.

    Holes, patterns and slots used to be copied whole from the pass with the
    most of them, unchecked — a hole one pass out of three imagined reached the
    kernel, and the corner radius and the sketch were not carried at all (a
    sketch profile came out as ``shape="sketch"`` with no edges). Now they are
    voted like every other cut feature.
    """
    if seen < minimum:
        return None, (f"контур прочитан только в {seen} из {total} проходов"), []
    merged: dict[str, Any] = {}
    shape, _shape_votes = _vote_text([p.get("shape") for p in profiles], minimum=minimum)
    if shape is None:
        return None, "проходы не сошлись на форме контура", []
    merged["shape"] = shape
    same_shape = [p for p in profiles if _text_key(p.get("shape")) == _text_key(shape)]
    for field in ("width_mm", "height_mm", "diameter_mm", "thickness_mm"):
        value, _votes = _vote_number([p.get(field) for p in same_shape], minimum=minimum)
        merged[field] = value
    if shape == "rectangle":
        radii = [p.get("corner_radius_mm") for p in same_shape]
        radius, _votes = _vote_number(radii, minimum=minimum)
        if radius is not None:
            merged["corner_radius_mm"] = radius
        elif any(value is not None for value in radii):
            return None, "проходы не сошлись на радиусе углов контура", []
    if shape == "sketch":
        sketch = _agreed_sketch([p.get("sketch") or [] for p in same_shape], minimum=minimum)
        if sketch is None:
            return None, "проходы не сошлись на эскизе контура", []
        merged["sketch"] = sketch
    notes: list[str] = []
    for field in ("holes", "hole_patterns", "slots"):
        reads = [p.get(field) or [] for p in same_shape]
        accepted, dropped = _vote_feature_list(reads, minimum=minimum)
        merged[field] = accepted
        if dropped and not accepted:
            return None, f"проходы не сошлись на элементах контура ({field})", []
        if dropped:
            notes.append(
                f"контур ({field}): {dropped} элемент(ов) подтверждено меньшинством "
                "проходов — не построено"
            )
    return merged, None, notes


def _sketches_agree(left: list[dict], right: list[dict]) -> bool:
    if len(left) != len(right):
        return False
    for a, b in zip(left, right, strict=True):
        if not isinstance(a, dict) or not isinstance(b, dict):
            return False
        if a.get("kind") != b.get("kind") or a.get("clockwise") != b.get("clockwise"):
            return False
        for key in ("to", "center"):
            pa, pb = a.get(key), b.get(key)
            if pa is None and pb is None:
                continue
            if pa is None or pb is None or len(pa) != len(pb):
                return False
            if not all(_numbers_agree(x, y) for x, y in zip(pa, pb, strict=True)):
                return False
    return True


def _agreed_sketch(reads: list[list[dict]], *, minimum: int) -> list[dict] | None:
    """One pass's whole sketch, if enough passes drew the same loop.

    Taken whole, never merged edge by edge: a loop stitched from different
    passes is a contour none of them saw, and it need not even close.
    """
    for candidate in reads:
        if not candidate:
            continue
        votes = sum(1 for other in reads if _sketches_agree(candidate, other))
        if votes >= minimum:
            return candidate
    return None


def consensus_spec(specs: list[dict], *, minimum: int | None = None) -> dict:
    """Intersect several reads of the same sheet into one conservative spec.

    The result carries a ``consensus`` block describing what agreed, and every
    disagreement is appended to ``unresolved`` so the fail-closed contract stops
    construction exactly where the reads stopped agreeing.

    ``minimum`` defaults to :func:`required_agreement` of the usable passes —
    a strict majority of the reads that came back, not a fixed 2. Callers (and
    tests) may still pin it explicitly.
    """
    usable = [spec for spec in specs if isinstance(spec, dict) and spec]
    if not usable:
        return {}
    if minimum is None:
        minimum = required_agreement(len(usable))
    if len(usable) == 1:
        merged = dict(usable[0])
        merged["consensus"] = {
            "passes": 1,
            "usable": 1,
            "agreement": "single_pass",
        }
        merged["value_provenance"] = _build_value_provenance(merged, usable)
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
    main, main_problems, feature_notes = _body_consensus(
        main_bodies, minimum=minimum, total=total, label="главный вид"
    )
    merged["main_view"] = main
    disagreements.extend(main_problems)
    parts, part_problems, part_notes = _parts_consensus(usable, minimum=minimum, total=total)
    disagreements.extend(part_problems)
    feature_notes.extend(part_notes)

    # Title-block metadata never blocks geometry, so a disagreement here is
    # optional rather than fatal.
    optional: list[str] = list(optional_conflicts) + feature_notes
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
    merged["parts"] = parts
    if usable[0].get("source_images"):
        merged["source_images"] = usable[0]["source_images"]

    # Anything a single pass declared unresolved stays unresolved: one reader
    # admitting it could not prove a value is enough to keep it out.
    inherited = sorted(
        {str(item) for spec in usable for item in (spec.get("unresolved") or []) if str(item)}
    )
    merged["unresolved"] = sorted(set(disagreements) | set(inherited))
    merged["optional_unresolved"] = sorted(
        set(optional)
        | {
            str(item)
            for spec in usable
            for item in (spec.get("optional_unresolved") or [])
            if str(item)
        }
    )
    merged["consensus"] = {
        "passes": len(specs),
        "usable": total,
        "minimum_agreement": minimum,
        "part_votes": part_votes,
        "disagreements": disagreements,
    }
    merged["value_provenance"] = _build_value_provenance(merged, usable)
    return merged


def _parts_consensus(
    usable: list[dict], *, minimum: int, total: int
) -> tuple[list[dict], list[str], list[str]]:
    """Additional bodies, voted body by body.

    ``parts`` used to be reset to ``[]`` whenever two or more passes came
    back, so a multi-body read survived only as a single pass and was lost the
    moment consensus actually ran. The passes must first agree on HOW MANY
    bodies there are; then each body is voted like the main one, matched by
    position (by name when every pass named every body).
    """
    reads = [
        [part for part in (spec.get("parts") or []) if isinstance(part, dict)] for spec in usable
    ]
    if not any(reads):
        return [], [], []
    count, votes = _vote_number([len(read) for read in reads], minimum=minimum)
    if count is None or count == 0:
        return (
            [],
            [
                f"дополнительные тела: проходы не сошлись на их числе ({sorted({len(r) for r in reads})})"
            ],
            [],
        )
    count = int(count)
    agreeing = [read for read in reads if len(read) == count]
    if all(_text_key(part.get("name")) for read in agreeing for part in read):
        agreeing = [sorted(read, key=lambda part: _text_key(part.get("name"))) for read in agreeing]
    parts: list[dict] = []
    problems: list[str] = []
    notes: list[str] = []
    for index in range(count):
        body, body_problems, body_notes = _body_consensus(
            [read[index] for read in agreeing],
            minimum=min(minimum, len(agreeing)),
            total=total,
            label=f"тело {index + 2}",
        )
        parts.append(body)
        problems.extend(body_problems)
        notes.extend(body_notes)
    return parts, problems, notes


def _agreed_items(reads: list[list[dict]], key: str, *, minimum: int) -> list[dict]:
    """Keep list items whose key text appears in at least ``minimum`` passes."""
    counts: Counter[str] = Counter()
    first: dict[str, dict] = {}
    for read in reads:
        seen_here: set[str] = set()
        for item in read:
            if not isinstance(item, dict):
                continue
            if key == "kind" and (item.get("view_id") or item.get("label")):
                text = _text_key(f"{item.get(key)}|{item.get('view_id') or item.get('label')}")
            else:
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
