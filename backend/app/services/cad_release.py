"""Reproducible release package for an accepted CAD drawing (C5).

A release manifest ties together everything a downstream consumer needs to
trust and reproduce a drawing: the CAD IR revision and its content hash, the
derived artifact hashes, the deterministic re-render check (prove the DXF/SVG
regenerate byte-identically from the stored IR), the ЕСКД validation report
(with the profile version that judged it), and the approval trail. The whole
manifest is itself hashed so a release has one stable identity.

The release is BLOCKED unless the drawing is accepted at the current
revision and carries no blocking (error-severity) ЕСКД/geometry issue — the
same boundary ``accept-vectorize`` enforces, re-checked here so a manifest
can never describe an unreleasable drawing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.ai.cad_ir.dxf_render import render_ir_to_dxf
from app.ai.cad_ir.png_render import render_ir_to_png
from app.ai.cad_ir.schema import CadIR
from app.ai.cad_ir.svg_render import render_ir_to_svg

MANIFEST_VERSION = "1.0"
DXF_VERSION = "R2010"


class ReleaseBlocked(ValueError):
    """The drawing cannot be released yet (not accepted / blocking issues)."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_release_manifest(
    *,
    generation_id: str,
    revision: int,
    ir: CadIR,
    stored_ir_sha256: str | None,
    stored_artifact_hashes: dict[str, str],
    accepted: bool,
    accepted_by: str | None,
    accepted_at: str | None,
    accepted_revision: int | None,
    approved_by: str | None,
    approved_at: str | None,
) -> dict[str, Any]:
    """Assemble (and self-verify) the release manifest for one accepted CAD IR
    revision. Raises ``ReleaseBlocked`` when the drawing is not releasable."""
    blocking = [
        {"code": i.code, "rule_id": i.rule_id, "message_ru": i.message_ru,
         "norm_ref": i.norm_ref, "entity_ids": i.entity_ids}
        for i in ir.validation.blocking
    ]
    if not accepted or accepted_revision != revision:
        raise ReleaseBlocked(
            "Чертёж не принят на текущей ревизии — примите его перед выпуском."
        )
    if blocking:
        raise ReleaseBlocked(
            f"В отчёте валидации {len(blocking)} блокирующих нарушений — "
            "выпуск невозможен."
        )

    # Re-render deterministically and prove reproducibility against what was
    # stored at save time. The IR is the source of truth; artifacts are
    # derived and must regenerate identically.
    ir_bytes = ir.model_dump_json().encode("utf-8")
    rerender = {
        "dxf": _sha(render_ir_to_dxf(ir)),
        "svg": _sha(render_ir_to_svg(ir)),
        "png": _sha(render_ir_to_png(ir)),
    }
    reproducible = {
        kind: (stored_artifact_hashes.get(kind) == rerender[kind])
        for kind in rerender
    }
    ir_reproducible = stored_ir_sha256 == _sha(ir_bytes)

    counts: dict[str, int] = {}
    for e in ir.entities:
        counts[e.type] = counts.get(e.type, 0) + 1

    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "generation_id": generation_id,
        "revision": revision,
        "dxf_version": DXF_VERSION,
        "cad_ir": {
            "sha256": stored_ir_sha256,
            "reproducible": ir_reproducible,
            "scale_mm_per_px": ir.scale,
            "scale_source": ir.scale_source,
            "recognizer_used": ir.recognizer_used,
            "entity_counts": counts,
            "sheet_format": ir.sheet.format,
        },
        "artifacts": {
            kind: {
                "stored_sha256": stored_artifact_hashes.get(kind),
                "rerender_sha256": rerender[kind],
                "reproducible": reproducible[kind],
            }
            for kind in rerender
        },
        "validation": {
            "eskd_profile_version": ir.validation.eskd_profile_version,
            "coverage_recall": ir.validation.coverage_recall,
            "coverage_precision": ir.validation.coverage_precision,
            "blocking": blocking,  # empty here by construction
            "codes": sorted({i.code for i in ir.validation.issues}),
            "issue_count": len(ir.validation.issues),
        },
        "approval": {
            "accepted": accepted,
            "accepted_by": accepted_by,
            "accepted_at": accepted_at,
            "accepted_revision": accepted_revision,
            "approved_by": approved_by,
            "approved_at": approved_at,
        },
    }
    manifest["fully_reproducible"] = ir_reproducible and all(reproducible.values())
    # The manifest's own identity: a hash over its canonical JSON (excluding
    # this field), so a release is referenceable by one stable digest.
    manifest["manifest_sha256"] = _sha(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    return manifest
