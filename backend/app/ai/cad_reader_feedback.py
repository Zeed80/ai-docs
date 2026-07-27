"""Capture what the reader got wrong, so it can eventually be taught.

Item 6 of the remediation plan is fine-tuning the reader on our own accepted
corrections. Before any training there has to be a corpus, and the corpus we
need does not exist: the existing self-learning exporter collects GEOMETRY
corrections (CAD IR revision 0 against the human-edited revision), which is
the image-to-geometry target that already failed this project once at entity
F1 0.000. What beats frontier models on drawings — Florence-2 at 0.23B does,
by a reported 52% F1 — is FIELD EXTRACTION, and nothing was recording whether
a field the reader produced was right.

So this records exactly that: for one digitization, what the reader said and
what a person corrected it to, field by field. It is deliberately small and
boring — a training corpus is only as trustworthy as the moment of capture,
and a clever inference here would poison every model trained on it later.

Nothing is inferred, nothing is auto-accepted: a correction exists only when a
human states it.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

# Fields worth learning: everything the reader is asked to extract, keyed by
# where it lives in the spec. Geometry lists are compared wholesale because a
# section is only meaningful in order.
_SCALAR_FIELDS = (
    ("part", ("part",)),
    ("material", ("title_block", "material")),
    ("designation", ("title_block", "designation")),
    ("scale", ("title_block", "scale")),
    ("mass", ("title_block", "mass")),
    ("body_type", ("main_view", "type")),
)
_LIST_FIELDS = (
    ("outer", ("main_view", "outer")),
    ("bore", ("main_view", "bore")),
    ("profile", ("main_view", "profile")),
    ("dimensions", ("dimensions",)),
    ("annotations", ("annotations",)),
    ("views", ("views",)),
    # The features a real part has. Correctable for the same reason the contour
    # is: a keyway the reader placed 40 mm off is one number away from right,
    # and the correction is worth more as a training pair than as a lost part.
    ("chamfers", ("main_view", "chamfers")),
    ("grooves", ("main_view", "grooves")),
    ("keyways", ("main_view", "keyways")),
    ("cross_holes", ("main_view", "cross_holes")),
)


def _get(spec: dict, path: tuple[str, ...]) -> Any:
    node: Any = spec
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def diff_spec(read: dict, corrected: dict) -> dict[str, Any]:
    """Field-level difference between what was read and what a human confirmed.

    ``unchanged`` matters as much as ``changed``: a corpus of only mistakes
    teaches a model that everything is a mistake.
    """
    changed: dict[str, dict[str, Any]] = {}
    unchanged: list[str] = []
    for name, path in _SCALAR_FIELDS + _LIST_FIELDS:
        before, after = _get(read, path), _get(corrected, path)
        if before == after:
            if after not in (None, "", [], {}):
                unchanged.append(name)
            continue
        changed[name] = {"read": before, "corrected": after}
    return {
        "changed": changed,
        "unchanged": unchanged,
        "changed_count": len(changed),
        "confirmed_count": len(unchanged),
    }


def build_correction_record(
    *,
    generation_id: str,
    source_path: str | None,
    read_spec: dict,
    corrected_spec: dict,
    corrected_by: str | None,
    reader_models: list[str] | None = None,
) -> dict[str, Any]:
    """One training example: the sheet, the read, the correction, the provenance.

    ``source_path`` points at the normalized image in object storage rather
    than embedding it, so the record stays small and the corpus can be rebuilt
    against the exact bytes that were read.
    """
    return {
        "generation_id": generation_id,
        "source_path": source_path,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "corrected_by": corrected_by,
        "reader_models": reader_models or [],
        "read_spec": read_spec,
        "corrected_spec": corrected_spec,
        "diff": diff_spec(read_spec, corrected_spec),
    }


def merge_correction(read_spec: dict, correction: dict) -> dict:
    """Apply a human's field corrections onto a read spec.

    Only the fields the person actually supplied are touched; everything else
    keeps what the reader produced, so a correction of one diameter does not
    quietly discard the rest of the sheet.
    """
    # A DEEP copy: a shallow one shares the nested dicts with the read spec, so
    # writing a correction into it silently rewrote the original too — which
    # destroys the "before" half of the training pair this module exists to
    # preserve. Caught by a live correction that recorded zero changes.
    merged = copy.deepcopy(read_spec or {})
    for name, path in _SCALAR_FIELDS + _LIST_FIELDS:
        if name not in correction:
            continue
        value = correction[name]
        node = merged
        for key in path[:-1]:
            child = node.get(key)
            if not isinstance(child, dict):
                child = {}
                node[key] = child
            node = child
        node[path[-1]] = value
    return merged


def corpus_summary(records: list[dict]) -> dict[str, Any]:
    """What a corpus actually contains, before anyone trains on it.

    A count of examples says nothing about whether they cover the failures that
    matter; a per-field tally does.
    """
    per_field: dict[str, int] = {}
    confirmed: dict[str, int] = {}
    for record in records:
        diff = record.get("diff") or {}
        for name in (diff.get("changed") or {}):
            per_field[name] = per_field.get(name, 0) + 1
        for name in diff.get("unchanged") or []:
            confirmed[name] = confirmed.get(name, 0) + 1
    return {
        "records": len(records),
        "corrections_per_field": dict(sorted(
            per_field.items(), key=lambda item: -item[1]
        )),
        "confirmations_per_field": dict(sorted(
            confirmed.items(), key=lambda item: -item[1]
        )),
        "sheets_with_any_correction": sum(
            1 for r in records if (r.get("diff") or {}).get("changed_count")
        ),
    }
