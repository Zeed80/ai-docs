"""Celery task: vectorize a scanned/photographed drawing into CAD IR + DXF.

The deterministic core of the «Точный чертёж» mode:

    source image → classical preprocess (dewarp/deskew/denoise/CLAHE)
    → binarize (Otsu + speck removal + gap closing)
    → recognize primitives: neural seq2seq arbitrated against CV by
      independent coverage scoring (cad_recognize.verify.arbitrate_recognition) —
      never picked on the model's own confidence
    → OCR text regions → TextEntity annotations (+ excluded from stroking)
    → sheet frame detection → px→mm scale (or SCALE_UNKNOWN)
    → assemble CAD IR → independent coverage verification → validate
    → render PNG/SVG/DXF, store IR revision 0

No diffusion and no LLM anywhere in this path. When a photo is too dirty,
the user first runs the existing diffusion *cleanup* operation and then
vectorizes its result (the composer already supports generation-as-source) —
the two pipelines stay composable instead of entangled.

CPU-only: no GPU lock, no ComfyUI dependency, so it runs in the default
Celery queue and works when the GPU is busy training LoRA.
"""

from __future__ import annotations

import math
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.tasks.async_runner import run_async
from app.tasks.celery_app import celery_app

# ГОСТ 2.104 stamp height in the drafter's paper-space pixels (55 mm × 4 px/mm).
_TITLE_BLOCK_H_MM_PX = 55.0 * 4.0

logger = structlog.get_logger()

_CAD_PROCESS_LOG_VERSION = 1
_CAD_PROCESS_LOG_MAX_EVENTS = 500
_CAD_MODEL_OUTPUT_MAX_ITEMS = 160


def _progress_for_event(
    stage: str,
    status: str,
    details: dict[str, Any],
    current: int,
) -> int:
    """Monotonic user-facing progress derived from durable stage events."""

    explicit = details.pop("_progress_pct", None)
    if explicit is not None:
        try:
            return max(current, min(100, int(explicit)))
        except (TypeError, ValueError):
            pass
    if stage == "pipeline" and status == "started":
        return max(current, 1)
    if stage.startswith("source."):
        return max(current, 3)
    if stage == "pipeline.manifest":
        return max(current, 5)
    if stage == "reader" and status == "started":
        return max(current, 7)
    if stage == "reader" and status == "completed":
        return max(current, 62)
    if stage == "reader.type_gate":
        return max(current, 65)
    if stage == "reader.followup":
        return max(current, 70 if status == "completed" else 66)
    if stage == "normalize.verify":
        return max(current, 76)
    if stage.startswith("solid."):
        return max(current, 82)
    if stage == "kernel.compile":
        return max(current, 90 if status == "completed" else 84)
    if stage == "kernel.verify":
        return max(current, 93)
    if stage.startswith("sheet."):
        return max(current, 96)
    if stage == "pipeline" and status == "completed":
        return 100
    return current


async def _append_cad_process_event(
    gen_uuid: uuid.UUID,
    stage: str,
    status: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Persist one live CAD pipeline event without owning the task session.

    A fresh short session makes every event visible to polling clients even if
    the worker is later killed by a hard/soft time limit.  Observability must
    never be able to fail the drawing itself, hence the best-effort boundary.
    """

    from app.db.models import ImageGeneration
    from app.db.session import _get_session_factory

    try:
        factory = _get_session_factory()
        async with factory() as db:
            gen = await db.get(ImageGeneration, gen_uuid)
            if gen is None:
                return
            params = dict(gen.params or {})
            process = dict(params.get("cad_process") or {})
            events = list(process.get("events") or [])
            now = datetime.now(UTC).isoformat()
            sequence = int(events[-1].get("sequence") or 0) + 1 if events else 1
            event_details = dict(details or {})
            model_output = event_details.pop("_model_output", None)
            partial_spec = event_details.pop("_partial_spec", None)
            if isinstance(model_output, dict):
                output_id = f"model-output-{sequence}"
                outputs = list(params.get("cad_model_outputs") or [])
                outputs.append({
                    "id": output_id,
                    "sequence": sequence,
                    "at": now,
                    "stage": stage,
                    **model_output,
                })
                params["cad_model_outputs"] = outputs[-_CAD_MODEL_OUTPUT_MAX_ITEMS:]
                event_details["model_output_id"] = output_id
            if isinstance(partial_spec, dict) and partial_spec:
                params["cad_partial_spec"] = partial_spec
                params["cad_partial_spec_sequence"] = sequence
            event = {
                "sequence": sequence,
                "at": now,
                "stage": stage,
                "status": status,
                "message": message,
                "details": event_details,
            }
            events.append(event)
            progress_pct = _progress_for_event(
                stage,
                status,
                event_details,
                int(process.get("progress_pct") or 0),
            )
            process.update({
                "version": _CAD_PROCESS_LOG_VERSION,
                "status": (
                    "failed" if stage == "pipeline" and status == "failed"
                    else "done" if stage == "pipeline" and status == "completed"
                    else process.get("status", "running")
                ),
                "current_stage": stage,
                "current_status": status,
                "updated_at": now,
                "progress_pct": progress_pct,
                "current_message": message,
                "events": events[-_CAD_PROCESS_LOG_MAX_EVENTS:],
            })
            process.setdefault("started_at", now)
            params["cad_process"] = process
            gen.params = params
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - telemetry cannot break CAD work
        logger.warning(
            "cad_process_event_store_failed",
            generation_id=str(gen_uuid),
            stage=stage,
            error=str(exc)[:200],
        )


async def _load_cad_partial_spec(gen_uuid: uuid.UUID) -> dict[str, Any]:
    """Load the last committed consensus after an outer reader timeout.

    This snapshot was captured mid-read (``_record``'s ``_partial_spec``,
    e.g. the fragment consensus logged before the whole-sheet fallback even
    started) — never through ``read_spec_best_effort``'s own return path, so
    it never got ``assign_stable_feature_ids``. A timed-out read is common
    enough on a dense multi-view sheet (a live run on detal_126.png hit this
    exact branch) that skipping the id pass here silently starved the native
    EMG builder and every SpecView.features_shown reference downstream of a
    stable id to point at — apply it here, once, idempotently, so a
    timeout-recovered spec is not a second-class read.
    """

    from app.ai.cad_recognize.spec_vectorize import assign_stable_feature_ids
    from app.db.models import ImageGeneration
    from app.db.session import _get_session_factory

    factory = _get_session_factory()
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        value = (gen.params or {}).get("cad_partial_spec") if gen else None
        return assign_stable_feature_ids(dict(value)) if isinstance(value, dict) else {}


async def _finalize_cad_task_failure(generation_id: str, message: str) -> None:
    """Make an out-of-coroutine Celery failure visible in API and UI."""

    from app.tasks.image_generation import _mark_failed

    gen_uuid = uuid.UUID(generation_id)
    await _append_cad_process_event(
        gen_uuid,
        "pipeline",
        "failed",
        message,
        {"terminal": True},
    )
    await _mark_failed(gen_uuid, message)

# ГОСТ 2.301 sheet sizes (portrait, mm) — landscape is matched by swapping.
_GOST_SHEETS = {
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "A2": (420.0, 594.0),
    "A1": (594.0, 841.0),
    "A0": (841.0, 1189.0),
}
_FRAME_LEFT_MARGIN_MM = 20.0
_FRAME_OTHER_MARGIN_MM = 5.0
_FRAME_MIN_AREA_FRACTION = 0.5
_FRAME_ASPECT_TOL = 0.06

# ГОСТ 2.104: основная надпись sits bottom-right — same bottom-15%×right-30%
# convention already used by drawing_cleanup._text_exclusion_boxes and
# drawing_preprocessor._detect_title_block; kept in sync deliberately.
_TITLE_BLOCK_WIDTH_RATIO = 0.30
_TITLE_BLOCK_HEIGHT_RATIO = 0.15
_TITLE_BLOCK_MIN_INK_FRACTION = 0.01


async def _editor_graph_base(
    db: Any, generation_id: str, *, lock: bool = False
) -> tuple[str, Any, bool]:
    """Use the design branch, falling back to the immutable read graph once."""
    from app.services.engineering_model_graph import latest_graph_revision

    source_graph_id = f"image-generation:{generation_id}"
    design_graph_id = f"{source_graph_id}:design"
    design = await latest_graph_revision(db, design_graph_id, lock=lock)
    if design is not None:
        return design_graph_id, design, False
    source = await latest_graph_revision(db, source_graph_id, lock=lock)
    return design_graph_id, source, True


async def _persist_editor_candidate(
    db: Any,
    *,
    generation_id: str,
    spec: dict[str, Any],
    base_candidate: Any,
    updated_candidate: Any,
    base_row: Any,
    pass_id: str,
    idempotency_key: str,
    source_sha256: str | None,
    source_uri: str | None,
    decision_note: str,
    actor_sub: str | None,
    fork_design_branch: bool,
) -> Any:
    """Fork once, then store the human edit as an audited atomic patch."""
    from app.services.engineering_model_graph import persist_feature_tree_revision

    design_graph_id = f"image-generation:{generation_id}:design"
    head = base_row
    if fork_design_branch:
        head = await persist_feature_tree_revision(
            db,
            graph_id=design_graph_id,
            spec=spec,
            candidate=base_candidate,
            producer="system",
            pass_id=f"design-fork:{base_row.graph_id}:r{base_row.revision}",
            idempotency_key=f"design-fork:{generation_id}:{base_row.canonical_sha256}",
            source_sha256=source_sha256,
            source_uri=source_uri,
        )
    return await persist_feature_tree_revision(
        db,
        graph_id=design_graph_id,
        spec=spec,
        candidate=updated_candidate,
        producer="human",
        pass_id=pass_id,
        idempotency_key=idempotency_key,
        source_sha256=source_sha256,
        source_uri=source_uri,
        expected_base_revision=head.revision,
        expected_base_sha256=head.canonical_sha256,
        decision_note=decision_note,
        actor_sub=actor_sub,
    )


async def _store_editor_build(
    *,
    factory: Any,
    gen_uuid: Any,
    graph_row: Any,
    solid_result: dict[str, Any],
    spec: dict[str, Any],
    owner_sub: str | None,
) -> None:
    """Publish one graph build as a new, approval-invalidating CAD revision.

    The editor used to replace only ``params.solid_3d``.  CadIR, acceptance
    and the top-level artifact pointers consequently remained on the previous
    revision.  Persisting the sheet through ``cad_ir_store`` makes the graph,
    2D projection and downloadable 3D artifacts advance together.
    """
    from app.ai.cad_ir.schema import CadIR
    from app.ai.cad_validate import validate_ir
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services import cad_ir_store

    sheet_ir = solid_result.pop("_sheet_ir", None)
    solid_result.pop("_dimensions", None)
    if not isinstance(sheet_ir, CadIR):
        raise ValueError("CAD build did not return a revision-bound drawing")
    sheet_ir.source.generation_id = str(gen_uuid)
    _overlay_spec_annotations(sheet_ir, spec)
    validate_ir(sheet_ir)
    sheet_ir.digitization_status = "review_required"

    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen is None:
            raise LookupError("not found")
        cad_revision = await cad_ir_store.save_revision(
            db,
            gen,
            sheet_ir,
            origin="editor",
            created_by=owner_sub,
            keep_raster=None,
        )
        cad_revision.engineering_graph_revision_id = graph_row.id
        paths = dict(solid_result.get("paths") or {})
        params = dict(gen.params or {})
        for key in (
            "full_check_revision",
            "full_check_status",
            "full_check_source_comparison",
        ):
            params.pop(key, None)
        for kind in ("step", "iges", "stl", "topology"):
            path = paths.get(kind)
            if path:
                params[f"{kind}_path"] = path
        params.update({
            "cad_artifact_revision": cad_revision.revision,
            "cad_edit_context": {
                "mode": "design",
                "source_graph_id": f"image-generation:{gen_uuid}",
                "design_graph_id": graph_row.graph_id,
            },
            "solid_3d": solid_result,
            "engineering_model_graph": {
                "revision_id": str(graph_row.id),
                "graph_id": graph_row.graph_id,
                "revision": graph_row.revision,
                "canonical_sha256": graph_row.canonical_sha256,
            },
        })
        gen.params = params
        gen.status = ImageGenStatus.done
        await db.commit()


@celery_app.task(
    bind=True,
    name="cad_trace.rebuild_from_spec",
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def rebuild_from_spec(
    self,
    generation_id: str,
    correction_event_id: str | None = None,
) -> dict:
    """Rebuild the part and its sheet from the CORRECTED spec — no model runs.

    Reading a sheet costs minutes of GPU and is where the mistakes come from;
    building from a complete reading is deterministic and cheap. Once a person
    has fixed a dimension, re-reading the drawing would be both slower and
    worse — it might come back with a different mistake. So this path starts
    from the stored spec and never looks at the image again.
    """
    return run_async(_rebuild_from_spec(generation_id, correction_event_id))


async def _rebuild_from_spec(
    generation_id: str,
    correction_event_id: str | None = None,
) -> dict:
    import uuid as _uuid

    from app.ai.cad_ir.schema import ValidationIssueIR
    from app.ai.cad_validate import validate_ir
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services import cad_ir_store

    factory = _get_session_factory()
    gen_uuid = _uuid.UUID(generation_id)
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen is None:
            return {"error": "not found"}
        params = dict(gen.params or {})
        owner_sub = gen.owner_sub
    current_correction_event_id = params.get("spec_correction_event_id")
    if (
        correction_event_id is not None
        and current_correction_event_id != correction_event_id
    ):
        return {
            "ok": True,
            "superseded": True,
            "generation_id": generation_id,
            "correction_event_id": correction_event_id,
        }
    spec = params.get("spec_corrected") or params.get("spec")
    if not spec:
        return {"error": "нет сохранённой спецификации для пересборки"}

    from app.ai.cad_recognize.spec_assumptions import apply_assumptions

    spec, assumptions = apply_assumptions(_revalidated_spec(spec))
    from app.ai.cad_dimension_graph import build_dimension_graph

    dimension_graph = build_dimension_graph(spec)
    if dimension_graph["errors"]:
        spec = {
            **spec,
            "unresolved": list(dict.fromkeys([
                *(spec.get("unresolved") or []),
                *dimension_graph["errors"],
            ])),
        }
    sheet_format = str(params.get("sheet_format") or "").upper() or None
    landscape = str(params.get("sheet_orientation") or "landscape").lower() != "portrait"
    engineering_graph = None
    engineering_graph_row = None
    engineering_graph_ref = None
    from app.config import settings as _settings

    if _settings.emg_enabled_for("mechanical"):
        import hashlib
        import json

        from app.ai.cad_solid import feature_tree_from_spec
        from app.services.engineering_model_graph import (
            load_graph,
            persist_feature_tree_revision,
        )

        rebuild_candidate = feature_tree_from_spec(spec)
        if rebuild_candidate is not None:
            patch_digest = hashlib.sha256(json.dumps(
                {
                    "spec": spec,
                    "candidate": rebuild_candidate.model_dump(mode="json"),
                    "human": bool(params.get("spec_corrected")),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode()).hexdigest()
            patch_key = (
                f"spec-correction:{generation_id}:{correction_event_id}"
                if correction_event_id is not None
                else f"spec-rebuild:{generation_id}:{patch_digest}"
            )
            async with factory() as db:
                engineering_graph_row = await persist_feature_tree_revision(
                    db,
                    graph_id=f"image-generation:{generation_id}",
                    spec=spec,
                    candidate=rebuild_candidate,
                    producer="human" if params.get("spec_corrected") else "system",
                    pass_id=(
                        f"spec-correction:{correction_event_id}"
                        if correction_event_id is not None
                        else f"spec-rebuild:{generation_id}"
                    ),
                    idempotency_key=patch_key,
                    source_sha256=params.get("normalized_source_sha256"),
                    source_uri=params.get("normalized_source_path"),
                )
                engineering_graph = load_graph(engineering_graph_row)
                engineering_graph_ref = {
                    "revision_id": str(engineering_graph_row.id),
                    "graph_id": engineering_graph_row.graph_id,
                    "revision": engineering_graph_row.revision,
                    "canonical_sha256": engineering_graph_row.canonical_sha256,
                }
                await db.commit()
    solid_result = await _build_spec_solid(
        spec, generation_id, owner_sub,
        sheet_format=sheet_format,
        landscape=landscape,
        # A plain retry must not bypass the raster evidence gate.  A stored
        # human correction is the explicit review action that may replace
        # missing model evidence for the corrected candidate.
        require_source_evidence=not bool(params.get("spec_corrected")),
        source_sha256=params.get("normalized_source_sha256"),
        source_uri=params.get("normalized_source_path"),
        engineering_graph_override=engineering_graph,
    )
    if not solid_result or not solid_result.get("built"):
        return {
            "error": (solid_result or {}).get("error") or "деталь не собралась",
            "built": False,
        }
    from app.ai.cad_source_projection import evaluate_source_projection

    solid_result["source_projection_verification"] = evaluate_source_projection(
        spec, params.get("spec_crosscheck") or {}, solid_result
    )
    result_graph = solid_result.pop("_engineering_model_graph", None)
    if engineering_graph is not None and (
        result_graph is None
        or result_graph.canonical_sha256 != engineering_graph.canonical_sha256
    ):
        return {"error": "CAD build returned a different EMG revision", "built": False}
    spec_ir = solid_result.pop("_sheet_ir", None)
    if spec_ir is None:
        return {"error": "CAD-ядро не построило лист", "built": False}
    spec_ir.source.generation_id = generation_id
    _overlay_spec_annotations(spec_ir, spec)
    dim_check = _unplaced_callouts(solid_result, spec)
    solid_result.pop("_dimensions", None)
    validate_ir(spec_ir)
    if dim_check.get("status") != "ok":
        spec_ir.validation.issues.append(
            ValidationIssueIR(
                code="SPEC_CALLOUTS_UNPLACED",
                severity="error",
                level=2,
                message_ru=(
                    "Не все геометрические размеры исходника размещены: "
                    + ", ".join(dim_check.get("unplaced") or [])
                ),
                fix_hint="Добавьте нужный вид/сечение или исправьте связь размера с feature",
            )
        )
    for assumption in assumptions:
        spec_ir.validation.issues.append(
            ValidationIssueIR(
                code=(
                    "SPEC_VALUE_DERIVED"
                    if assumption.origin == "derived"
                    else "SPEC_VALUE_ASSUMED"
                ),
                severity="warn",
                level=3,
                message_ru=(
                    f"{assumption.path}.{assumption.field} = "
                    f"{assumption.value:g} мм — {assumption.rule}"
                ),
                fix_hint="Проверьте по исходному листу и поправьте в редакторе",
            )
        )
    spec_ir.digitization_status = "review_required"

    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen is None:
            return {"error": "not found"}
        if (
            correction_event_id is not None
            and (gen.params or {}).get("spec_correction_event_id")
            != correction_event_id
        ):
            return {
                "ok": True,
                "superseded": True,
                "generation_id": generation_id,
                "correction_event_id": correction_event_id,
            }
        gen.params = {
            **(gen.params or {}),
            "spec": spec,
            "cad_reading": {
                "spec": spec,
                "unresolved": list(spec.get("unresolved") or []),
                "assumptions": [item.as_dict() for item in assumptions],
                "dimension_graph": dimension_graph,
                "source": "human_correction" if params.get("spec_corrected") else "stored_read",
            },
            "spec_dimension_check": dim_check,
            "dimension_graph": dimension_graph,
            "spec_assumptions": [item.as_dict() for item in assumptions],
            "solid_input": solid_result.get("kernel_input"),
            "solid_3d": solid_result,
            "rebuilt_from_spec": True,
            **(
                {"engineering_model_graph": engineering_graph_ref}
                if engineering_graph_ref is not None else {}
            ),
        }
        # A rebuild is a new REVISION, never an overwrite: the reader's own
        # attempt and the corrected one are both part of the record.
        cad_revision = await cad_ir_store.save_revision(
            db, gen, spec_ir, origin="human", created_by=owner_sub,
            keep_raster=None, thin_px=2, thick_px=4,
        )
        if engineering_graph_row is not None:
            cad_revision.engineering_graph_revision_id = engineering_graph_row.id
        gen.status = ImageGenStatus.done
        await db.commit()
    return {
        "ok": True,
        "generation_id": generation_id,
        "correction_event_id": correction_event_id,
        "entities": len(spec_ir.entities),
        "assumptions": len(assumptions),
    }


@celery_app.task(
    bind=True,
    name="cad_trace.add_feature_to_graph",
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def add_feature_to_graph(
    self,
    generation_id: str,
    feature_kind: str,
    feature_params: dict,
    note: str,
    idempotency_key: str,
    actor_sub: str | None = None,
    expected_base_revision: int | None = None,
    expected_base_sha256: str | None = None,
) -> dict:
    """Ф2 нового CAD-редактора (/root/.claude/plans/starry-mapping-hippo.md):
    add one human-authored BuildOperation on top of the graph's CURRENT
    state and recompile immediately.

    Deliberately NOT built on _rebuild_from_spec: that path always derives
    its candidate fresh from ``feature_tree_from_spec(spec)`` (see its own
    call above) — correct for "the drawing was misread, re-derive from the
    corrected numbers", but wrong here, since a feature added in the editor
    has no representation in ``spec`` at all and would be silently dropped
    on the very next spec-triggered rebuild. This task instead starts from
    ``feature_tree_from_graph`` — the graph's own current truth, corrections
    already included — appends one Feature3D, and seals+recompiles from
    that, exactly mirroring the old Cad3dPanel add-feature endpoint's
    Feature3D construction (_append_human_features in image_generation.py)
    but through the graph instead of the abandoned 2D-IR candidate index.
    """
    return run_async(
        _add_feature_to_graph(
            generation_id, feature_kind, feature_params, note, idempotency_key,
            actor_sub, expected_base_revision, expected_base_sha256,
        )
    )


async def _add_feature_to_graph(
    generation_id: str,
    feature_kind: str,
    feature_params: dict,
    note: str,
    idempotency_key: str,
    actor_sub: str | None = None,
    expected_base_revision: int | None = None,
    expected_base_sha256: str | None = None,
) -> dict:
    import uuid as _uuid

    from app.ai.cad_emg_compat import feature_tree_from_graph
    from app.ai.cad_ir.feature_tree import Feature3D, ParamProvenance
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services.engineering_model_graph import load_graph, persist_feature_tree_revision

    factory = _get_session_factory()
    gen_uuid = _uuid.UUID(generation_id)
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen is None:
            return {"error": "not found"}
        params = dict(gen.params or {})
        owner_sub = gen.owner_sub
        graph_id, latest_row, fork_design_branch = await _editor_graph_base(
            db, generation_id, lock=True
        )
    if latest_row is None:
        return {"error": "EngineeringModelGraph ещё не создан для этой генерации"}
    if (
        (expected_base_revision is not None and latest_row.revision != expected_base_revision)
        or (
            expected_base_sha256 is not None
            and latest_row.canonical_sha256 != expected_base_sha256
        )
    ):
        return {"error": "stale_graph_revision"}
    graph = load_graph(latest_row)
    spec = params.get("spec_corrected") or params.get("spec")
    if not spec:
        return {"error": "нет сохранённой спецификации"}

    base_candidate = feature_tree_from_graph(graph, target_id="preview")
    if base_candidate is None:
        return {"error": "не удалось прочитать текущее дерево построения из графа"}

    new_feature = Feature3D(
        kind=feature_kind,
        source_entity_ids=[],
        params=feature_params,
        # Ф3.3's own rule (_append_human_features): every param a human adds
        # is explicitly "human" provenance — never left unset.
        param_provenance={
            key: ParamProvenance(
                origin="human", detail="добавлено человеком в CAD-редакторе"
            )
            for key in feature_params
        },
        confidence=1.0,
    )
    updated_candidate = base_candidate.model_copy(deep=True)
    # Same single insertion point _append_human_features already uses: right
    # before the first "hole" (radial/axial holes are a shaft-specific,
    # position-sensitive class this editor does not add to yet — Ф3+).
    insert_at = next(
        (
            index
            for index, feature in enumerate(updated_candidate.features)
            if feature.kind == "hole"
        ),
        len(updated_candidate.features),
    )
    updated_candidate.features.insert(insert_at, new_feature)
    updated_candidate.label = f"{updated_candidate.label}; добавлена операция: {feature_kind}"

    async with factory() as db:
        engineering_graph_row = await _persist_editor_candidate(
            db,
            generation_id=generation_id,
            spec=spec,
            base_candidate=base_candidate,
            updated_candidate=updated_candidate,
            base_row=latest_row,
            pass_id=f"human-add-feature:{feature_kind}",
            idempotency_key=idempotency_key,
            source_sha256=params.get("normalized_source_sha256"),
            source_uri=params.get("normalized_source_path"),
            decision_note=note,
            actor_sub=actor_sub,
            fork_design_branch=fork_design_branch,
        )
        engineering_graph = load_graph(engineering_graph_row)
        await db.commit()

    solid_result = await _build_spec_solid(
        spec,
        generation_id,
        owner_sub,
        sheet_format=str(params.get("sheet_format") or "").upper() or None,
        landscape=str(params.get("sheet_orientation") or "landscape").lower()
        != "portrait",
        require_source_evidence=not bool(params.get("spec_corrected")),
        source_sha256=params.get("normalized_source_sha256"),
        source_uri=params.get("normalized_source_path"),
        engineering_graph_override=engineering_graph,
        # A human-added feature is EXPECTED to grow the envelope/volume past
        # the read profile (a boss on the end face; any additive geometry) —
        # verify_solid_against_spec's length/diameter/volume-above-profile
        # checks assume the opposite and would reject every legitimate
        # addition. Topology and feature_complete still gate.
        require_envelope_match=False,
    )
    if not solid_result or not solid_result.get("built"):
        error_message = (solid_result or {}).get("error") or "деталь не собралась"
        # Фаза 6 (/root/.claude/plans/starry-mapping-hippo.md): a failed
        # add-feature attempt must never become the graph's permanent
        # "latest" revision — before this fix, latest_graph_revision (which
        # EVERY graph read resolves against, not gen.params) kept loading
        # this broken revision as the base for every subsequent operation,
        # requiring a manual docker-exec cleanup script 3+ times this
        # session. base_candidate is exactly the state before this attempt
        # (updated_candidate = base_candidate + the one feature that just
        # failed to build) — re-persisting it as a fresh revision restores
        # "latest" to last-known-good. The failure itself is still fully
        # surfaced via `error` below; only the graph's own bookkeeping heals.
        async with factory() as db:
            try:
                rollback_row = await persist_feature_tree_revision(
                    db,
                    graph_id=graph_id,
                    spec=spec,
                    candidate=base_candidate,
                    producer="human",
                    pass_id=f"human-add-feature-rollback:{feature_kind}",
                    idempotency_key=f"{idempotency_key}:rollback",
                    source_sha256=params.get("normalized_source_sha256"),
                    source_uri=params.get("normalized_source_path"),
                )
                # The rollback revision's CONTENT is identical to whatever
                # gen.params.solid_3d was already built from — only the
                # revision number moved. Point gen.params at it too, or
                # "latest" (rollback_row) and "current" (gen.params) stay
                # out of sync exactly like the original bug, just one
                # level removed: the next add/remove would see the tree's
                # own operation_id prefix as "stale" against the new
                # latest and be refused for no real reason.
                gen_for_rollback = await db.get(ImageGeneration, gen_uuid)
                if gen_for_rollback is not None:
                    current_emg = dict(
                        (gen_for_rollback.params or {}).get("engineering_model_graph")
                        or {}
                    )
                    if current_emg:
                        gen_for_rollback.params = {
                            **(gen_for_rollback.params or {}),
                            "engineering_model_graph": {
                                **current_emg,
                                "revision_id": str(rollback_row.id),
                                "revision": rollback_row.revision,
                                "canonical_sha256": rollback_row.canonical_sha256,
                            },
                        }
                await db.commit()
            except Exception:
                # Best-effort: the original build error is the one that
                # matters to the caller — a failed rollback (e.g. a genuine
                # idempotency collision) must not mask it.
                pass
        return {"error": error_message, "built": False}
    result_graph = solid_result.pop("_engineering_model_graph", None)
    if result_graph is None or result_graph.canonical_sha256 != engineering_graph.canonical_sha256:
        return {"error": "CAD build returned a different EMG revision", "built": False}
    await _store_editor_build(
        factory=factory,
        gen_uuid=gen_uuid,
        graph_row=engineering_graph_row,
        solid_result=solid_result,
        spec=spec,
        owner_sub=owner_sub,
    )
    return {
        "ok": True,
        "generation_id": generation_id,
        "feature_kind": feature_kind,
        "note": note,
    }


def _parse_operation_id(operation_id: str) -> tuple[str, int]:
    """``operation:{prefix}:{index:04d}`` — the exact format
    ``feature_tree_revision_patch`` stamps on every BuildOperation node
    (cad_emg_compat.py, ``prefix = f"r{next_revision}"``). Raises
    ``ValueError`` on anything else — never guesses an index from a
    malformed id."""
    parts = operation_id.split(":")
    if len(parts) != 3 or parts[0] != "operation":
        raise ValueError(f"неверный формат id операции: {operation_id!r}")
    prefix, index_raw = parts[1], parts[2]
    if not index_raw.isdigit():
        raise ValueError(f"неверный формат id операции: {operation_id!r}")
    return prefix, int(index_raw)


@celery_app.task(
    bind=True,
    name="cad_trace.remove_feature_from_graph",
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def remove_feature_from_graph(
    self,
    generation_id: str,
    operation_id: str,
    note: str,
    idempotency_key: str,
    actor_sub: str | None = None,
    expected_base_revision: int | None = None,
    expected_base_sha256: str | None = None,
) -> dict:
    """Фаза 6 нового CAD-редактора: remove ONE BuildOperation from the
    graph's current state and recompile. Mirrors add_feature_to_graph in
    shape (feature_tree_from_graph + persist_feature_tree_revision +
    _build_spec_solid, all reused unchanged) — the removal mechanism
    itself (a corrected candidate.features list superseding the old
    operation.kind assertions) already existed in feature_tree_revision_
    patch; this is the first endpoint to expose it as a single, targeted
    "remove this one operation" action instead of "resubmit the whole
    document" (see the plan's own note on this — it's the exact operation
    every manual cleanup script this session performed by hand).
    """
    return run_async(
        _remove_feature_from_graph(
            generation_id, operation_id, note, idempotency_key,
            actor_sub, expected_base_revision, expected_base_sha256,
        )
    )


async def _remove_feature_from_graph(
    generation_id: str,
    operation_id: str,
    note: str,
    idempotency_key: str,
    actor_sub: str | None = None,
    expected_base_revision: int | None = None,
    expected_base_sha256: str | None = None,
) -> dict:
    import uuid as _uuid

    from app.ai.cad_emg_compat import feature_tree_from_graph
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services.engineering_model_graph import load_graph, persist_feature_tree_revision

    try:
        prefix, index = _parse_operation_id(operation_id)
    except ValueError as exc:
        return {"error": str(exc)}

    factory = _get_session_factory()
    gen_uuid = _uuid.UUID(generation_id)
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen is None:
            return {"error": "not found"}
        params = dict(gen.params or {})
        owner_sub = gen.owner_sub
        graph_id, latest_row, fork_design_branch = await _editor_graph_base(
            db, generation_id, lock=True
        )
    if latest_row is None:
        return {"error": "EngineeringModelGraph ещё не создан для этой генерации"}
    if (
        (expected_base_revision is not None and latest_row.revision != expected_base_revision)
        or (
            expected_base_sha256 is not None
            and latest_row.canonical_sha256 != expected_base_sha256
        )
    ):
        return {"error": "stale_graph_revision"}
    graph = load_graph(latest_row)
    # feature_tree_revision_patch stamps EVERY operation node with a fresh
    # prefix "r{graph.revision+1}" on every single persist (cad_emg_compat.py)
    # — so an operation_id's own prefix always equals the revision that
    # created it, which for the graph we just loaded as "latest" is exactly
    # f"r{graph.revision}". A mismatch means the graph moved on since the
    # tree the user is looking at was last loaded (a concurrent edit) —
    # refuse rather than delete the wrong feature by coincidental index
    # collision.
    expected_prefix = f"r{graph.revision}"
    if prefix != expected_prefix:
        return {
            "error": (
                "Дерево построения устарело (граф изменился с момента открытия) "
                "— обновите страницу и повторите."
            )
        }
    spec = params.get("spec_corrected") or params.get("spec")
    if not spec:
        return {"error": "нет сохранённой спецификации"}

    base_candidate = feature_tree_from_graph(graph, target_id="preview")
    if base_candidate is None:
        return {"error": "не удалось прочитать текущее дерево построения из графа"}
    if not (0 <= index < len(base_candidate.features)):
        return {"error": f"операция {operation_id} не найдена в текущем дереве"}

    updated_candidate = base_candidate.model_copy(deep=True)
    removed = updated_candidate.features.pop(index)
    updated_candidate.label = f"{updated_candidate.label}; удалена операция: {removed.kind}"

    async with factory() as db:
        engineering_graph_row = await _persist_editor_candidate(
            db,
            generation_id=generation_id,
            spec=spec,
            base_candidate=base_candidate,
            updated_candidate=updated_candidate,
            base_row=latest_row,
            pass_id=f"human-remove-feature:{removed.kind}",
            idempotency_key=idempotency_key,
            source_sha256=params.get("normalized_source_sha256"),
            source_uri=params.get("normalized_source_path"),
            decision_note=note,
            actor_sub=actor_sub,
            fork_design_branch=fork_design_branch,
        )
        engineering_graph = load_graph(engineering_graph_row)
        await db.commit()

    solid_result = await _build_spec_solid(
        spec,
        generation_id,
        owner_sub,
        sheet_format=str(params.get("sheet_format") or "").upper() or None,
        landscape=str(params.get("sheet_orientation") or "landscape").lower()
        != "portrait",
        require_source_evidence=not bool(params.get("spec_corrected")),
        source_sha256=params.get("normalized_source_sha256"),
        source_uri=params.get("normalized_source_path"),
        engineering_graph_override=engineering_graph,
        # Same reasoning as add_feature_to_graph: removal is a corrective
        # action, not a fresh read — the envelope/volume-vs-profile checks
        # don't apply here either way. Topology/feature_complete still gate.
        require_envelope_match=False,
    )
    if not solid_result or not solid_result.get("built"):
        error_message = (
            (solid_result or {}).get("error")
            or "деталь не собралась после удаления операции"
        )
        # Same auto-rollback principle as add_feature_to_graph's own failure
        # branch: a delete that leaves the model unbuildable (e.g. removing
        # geometry a later fillet's edge_key depended on) must not become
        # "latest" either — restore the pre-delete state.
        async with factory() as db:
            try:
                rollback_row = await persist_feature_tree_revision(
                    db,
                    graph_id=graph_id,
                    spec=spec,
                    candidate=base_candidate,
                    producer="human",
                    pass_id=f"human-remove-feature-rollback:{removed.kind}",
                    idempotency_key=f"{idempotency_key}:rollback",
                    source_sha256=params.get("normalized_source_sha256"),
                    source_uri=params.get("normalized_source_path"),
                )
                # Same reasoning as add_feature_to_graph's own rollback: keep
                # gen.params's "current" pointer in sync with "latest", or
                # the next add/remove sees a falsely-stale operation_id
                # prefix and gets refused for no real reason.
                gen_for_rollback = await db.get(ImageGeneration, gen_uuid)
                if gen_for_rollback is not None:
                    current_emg = dict(
                        (gen_for_rollback.params or {}).get("engineering_model_graph")
                        or {}
                    )
                    if current_emg:
                        gen_for_rollback.params = {
                            **(gen_for_rollback.params or {}),
                            "engineering_model_graph": {
                                **current_emg,
                                "revision_id": str(rollback_row.id),
                                "revision": rollback_row.revision,
                                "canonical_sha256": rollback_row.canonical_sha256,
                            },
                        }
                await db.commit()
            except Exception:
                pass
        return {"error": error_message, "built": False}
    result_graph = solid_result.pop("_engineering_model_graph", None)
    if result_graph is None or result_graph.canonical_sha256 != engineering_graph.canonical_sha256:
        return {"error": "CAD build returned a different EMG revision", "built": False}
    await _store_editor_build(
        factory=factory,
        gen_uuid=gen_uuid,
        graph_row=engineering_graph_row,
        solid_result=solid_result,
        spec=spec,
        owner_sub=owner_sub,
    )
    return {
        "ok": True,
        "generation_id": generation_id,
        "removed_kind": removed.kind,
        "note": note,
    }


def _pattern_offsets(pattern: dict, base_x: float, base_y: float) -> list[tuple[float, float]]:
    """Фаза 8: N (x, y) centres for one patterned feature — i=0 is always
    exactly the original position, matching the convention every real CAD
    tool uses ("the original IS the first instance", not a separate,
    un-patterned leftover). Pure arithmetic, no kernel/FreeCAD involved —
    this is the whole "backend-only pre-expansion" idea: the kernel never
    learns a pattern exists, only N ordinary features.
    """
    kind = pattern.get("kind")
    count = int(pattern["count"])
    if kind == "linear":
        dx, dy = float(pattern["dx_mm"]), float(pattern["dy_mm"])
        return [(base_x + i * dx, base_y + i * dy) for i in range(count)]
    if kind == "circular":
        cx, cy = float(pattern.get("center_x_mm") or 0.0), float(pattern.get("center_y_mm") or 0.0)
        total = float(pattern.get("total_angle_deg") or 360.0)
        step = math.radians(total / count)
        rel_x, rel_y = base_x - cx, base_y - cy
        offsets = []
        for i in range(count):
            angle = i * step
            cos_a, sin_a = math.cos(angle), math.sin(angle)
            offsets.append((
                cx + rel_x * cos_a - rel_y * sin_a,
                cy + rel_x * sin_a + rel_y * cos_a,
            ))
        return offsets
    raise ValueError(f"неизвестный вид массива: {kind!r}")


@celery_app.task(
    bind=True,
    name="cad_trace.pattern_feature_in_graph",
    max_retries=0,
    soft_time_limit=600,
    time_limit=660,
)
def pattern_feature_in_graph(
    self,
    generation_id: str,
    operation_id: str,
    pattern: dict,
    note: str,
    idempotency_key: str,
    actor_sub: str | None = None,
    expected_base_revision: int | None = None,
    expected_base_sha256: str | None = None,
) -> dict:
    """Фаза 8 нового CAD-редактора: replace ONE existing BuildOperation with
    N patterned copies (linear or circular), offsetting center_x_mm/
    center_y_mm. Mirrors remove_feature_from_graph's own shape exactly —
    same operation_id parsing, same prefix-staleness guard, same auto-
    rollback-on-failed-build. The only new arithmetic is _pattern_offsets
    above; everything else (persist/build/rollback) is the established
    pattern from Ф6/Ф2, reused unchanged."""
    return run_async(
        _pattern_feature_in_graph(
            generation_id, operation_id, pattern, note, idempotency_key,
            actor_sub, expected_base_revision, expected_base_sha256,
        )
    )


async def _pattern_feature_in_graph(
    generation_id: str,
    operation_id: str,
    pattern: dict,
    note: str,
    idempotency_key: str,
    actor_sub: str | None = None,
    expected_base_revision: int | None = None,
    expected_base_sha256: str | None = None,
) -> dict:
    import uuid as _uuid

    from app.ai.cad_emg_compat import feature_tree_from_graph
    from app.ai.cad_ir.feature_tree import Feature3D, ParamProvenance
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services.engineering_model_graph import load_graph, persist_feature_tree_revision

    try:
        prefix, index = _parse_operation_id(operation_id)
    except ValueError as exc:
        return {"error": str(exc)}

    factory = _get_session_factory()
    gen_uuid = _uuid.UUID(generation_id)
    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if gen is None:
            return {"error": "not found"}
        params = dict(gen.params or {})
        owner_sub = gen.owner_sub
        graph_id, latest_row, fork_design_branch = await _editor_graph_base(
            db, generation_id, lock=True
        )
    if latest_row is None:
        return {"error": "EngineeringModelGraph ещё не создан для этой генерации"}
    if (
        (expected_base_revision is not None and latest_row.revision != expected_base_revision)
        or (
            expected_base_sha256 is not None
            and latest_row.canonical_sha256 != expected_base_sha256
        )
    ):
        return {"error": "stale_graph_revision"}
    graph = load_graph(latest_row)
    expected_prefix = f"r{graph.revision}"
    if prefix != expected_prefix:
        return {
            "error": (
                "Дерево построения устарело (граф изменился с момента открытия) "
                "— обновите страницу и повторите."
            )
        }
    spec = params.get("spec_corrected") or params.get("spec")
    if not spec:
        return {"error": "нет сохранённой спецификации"}

    base_candidate = feature_tree_from_graph(graph, target_id="preview")
    if base_candidate is None:
        return {"error": "не удалось прочитать текущее дерево построения из графа"}
    if not (0 <= index < len(base_candidate.features)):
        return {"error": f"операция {operation_id} не найдена в текущем дереве"}

    target = base_candidate.features[index]
    if "center_x_mm" not in target.params or "center_y_mm" not in target.params:
        return {
            "error": (
                f"операция «{target.kind}» не имеет center_x_mm/center_y_mm "
                "— массив к ней неприменим"
            )
        }
    try:
        offsets = _pattern_offsets(
            pattern,
            float(target.params["center_x_mm"]),
            float(target.params["center_y_mm"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        return {"error": f"некорректные параметры массива: {exc}"}

    patterned = [
        Feature3D(
            kind=target.kind,
            source_entity_ids=list(target.source_entity_ids),
            params={**target.params, "center_x_mm": x, "center_y_mm": y},
            param_provenance={
                key: (
                    ParamProvenance(
                        origin="human",
                        detail=(
                            f"массив CAD-редактора, экземпляр "
                            f"{i + 1}/{len(offsets)}"
                        ),
                    )
                    if key in {"center_x_mm", "center_y_mm"}
                    else target.param_provenance.get(
                        key,
                        ParamProvenance(
                            origin="propagated",
                            detail="унаследовано от исходной операции массива",
                        ),
                    )
                )
                for key in target.params
            },
            confidence=1.0,
        )
        for i, (x, y) in enumerate(offsets)
    ]
    updated_candidate = base_candidate.model_copy(deep=True)
    updated_candidate.features[index:index + 1] = patterned
    updated_candidate.label = (
        f"{updated_candidate.label}; массив: {target.kind} × {len(patterned)}"
    )

    async with factory() as db:
        engineering_graph_row = await _persist_editor_candidate(
            db,
            generation_id=generation_id,
            spec=spec,
            base_candidate=base_candidate,
            updated_candidate=updated_candidate,
            base_row=latest_row,
            pass_id=f"human-pattern-feature:{target.kind}",
            idempotency_key=idempotency_key,
            source_sha256=params.get("normalized_source_sha256"),
            source_uri=params.get("normalized_source_path"),
            decision_note=note,
            actor_sub=actor_sub,
            fork_design_branch=fork_design_branch,
        )
        engineering_graph = load_graph(engineering_graph_row)
        await db.commit()

    solid_result = await _build_spec_solid(
        spec,
        generation_id,
        owner_sub,
        sheet_format=str(params.get("sheet_format") or "").upper() or None,
        landscape=str(params.get("sheet_orientation") or "landscape").lower()
        != "portrait",
        require_source_evidence=not bool(params.get("spec_corrected")),
        source_sha256=params.get("normalized_source_sha256"),
        source_uri=params.get("normalized_source_path"),
        engineering_graph_override=engineering_graph,
        # Same reasoning as add/remove-feature: a pattern is a corrective/
        # additive human action, not a fresh read.
        require_envelope_match=False,
    )
    if not solid_result or not solid_result.get("built"):
        error_message = (
            (solid_result or {}).get("error") or "деталь не собралась после создания массива"
        )
        async with factory() as db:
            try:
                rollback_row = await persist_feature_tree_revision(
                    db,
                    graph_id=graph_id,
                    spec=spec,
                    candidate=base_candidate,
                    producer="human",
                    pass_id=f"human-pattern-feature-rollback:{target.kind}",
                    idempotency_key=f"{idempotency_key}:rollback",
                    source_sha256=params.get("normalized_source_sha256"),
                    source_uri=params.get("normalized_source_path"),
                )
                gen_for_rollback = await db.get(ImageGeneration, gen_uuid)
                if gen_for_rollback is not None:
                    current_emg = dict(
                        (gen_for_rollback.params or {}).get("engineering_model_graph")
                        or {}
                    )
                    if current_emg:
                        gen_for_rollback.params = {
                            **(gen_for_rollback.params or {}),
                            "engineering_model_graph": {
                                **current_emg,
                                "revision_id": str(rollback_row.id),
                                "revision": rollback_row.revision,
                                "canonical_sha256": rollback_row.canonical_sha256,
                            },
                        }
                await db.commit()
            except Exception:
                pass
        return {"error": error_message, "built": False}
    result_graph = solid_result.pop("_engineering_model_graph", None)
    if result_graph is None or result_graph.canonical_sha256 != engineering_graph.canonical_sha256:
        return {"error": "CAD build returned a different EMG revision", "built": False}
    await _store_editor_build(
        factory=factory,
        gen_uuid=gen_uuid,
        graph_row=engineering_graph_row,
        solid_result=solid_result,
        spec=spec,
        owner_sub=owner_sub,
    )
    return {
        "ok": True,
        "generation_id": generation_id,
        "pattern_kind": target.kind,
        "instances": len(patterned),
        "note": note,
    }


@celery_app.task(
    bind=True,
    name="cad_trace.run_cad_trace",
    max_retries=2,
    soft_time_limit=600,
    time_limit=660,
)
def run_cad_trace(self, generation_id: str) -> dict:
    import time as _time

    from app.core import metrics

    started = _time.monotonic()
    try:
        result = run_async(_run(generation_id, self.request.id))
    except Exception as exc:
        metrics.cad_digitize_total.labels(status="error").inc()
        # A Celery soft limit can be raised while the event loop is polling,
        # outside ``_run`` and therefore outside its normal fail-closed handler.
        # Persist a terminal state here so the generation never remains
        # permanently ``running`` after the worker has stopped.
        message = (
            "Оцифровка остановлена по лимиту времени (600 с). "
            "Откройте журнал этапов: последний running-этап показывает место задержки."
            if type(exc).__name__ == "SoftTimeLimitExceeded"
            else f"{type(exc).__name__}: {exc}"
        )
        try:
            run_async(_finalize_cad_task_failure(generation_id, message))
        except Exception as persist_exc:  # noqa: BLE001
            logger.error(
                "cad_trace_terminal_failure_not_persisted",
                generation_id=generation_id,
                error=str(persist_exc)[:200],
            )
        raise
    finally:
        metrics.cad_digitize_duration_seconds.observe(_time.monotonic() - started)
    status = "error" if result.get("error") else ("declined" if result.get("declined") else "done")
    metrics.cad_digitize_total.labels(status=status).inc()
    return result


# A global Otsu threshold assumes a roughly bimodal histogram (clean dark
# ink on clean light paper) — it fails on foxed/stained/uneven-lit paper by
# reading the mottled staining itself as "ink", inflating density well past
# what real line-drawing content would ever produce. Confirmed live
# (2026-07-11, an aged diazo-print photo in test_vector_files): Otsu read
# ink_fraction=0.34 (above extract_primitives' 0.30 density-decline gate —
# the exact "лист слишком плотный" failure the user hit), while a local
# adaptive threshold on the SAME image read 0.14 and, after the same speck/
# gap hygiene, produced 1301 usable entities passing the full production
# coverage bar (recall 0.85, precision 1.0). Otsu is tried first and used
# whenever it's not egregiously dense — it is the simpler, more literal
# read of the ink and the 4 other test files all pass comfortably below the
# retry trigger, so this never touches an already-working image.
_OTSU_RETRY_INK_FRACTION = 0.22
# Sauvola is the last binarization resort: local mean/stddev thresholding
# survives severe uneven lighting where even the Gaussian adaptive retry
# stays implausibly dense (the CV density-decline gate is 0.30).
_SAUVOLA_RETRY_INK_FRACTION = 0.30
# B1 degrade-vs-fail split: an empty recognition on a sheet denser than this
# is garbage input (a near-solid photo), not a drawing to review.
_DEGRADED_MAX_INK_FRACTION = 0.85


def _binarize(image_bytes: bytes):
    """Otsu binarization + speck/gap hygiene (same recipe the cleanup
    postprocess uses), retried with local adaptive thresholding when Otsu
    alone reads implausibly dense (uneven lighting/staining, not real ink).
    Returns (ink uint8 mask 255=ink, w, h)."""
    import cv2
    import numpy as np

    from app.ai.drawing_cleanup import _close_small_gaps, _open_on_white, _remove_small_specks

    img = _open_on_white(image_bytes)
    arr = np.asarray(img)
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ink = cv2.bitwise_not(binary)
    ink = _remove_small_specks(ink, w * h)
    ink = _close_small_gaps(ink)

    if float((ink > 0).mean()) > _OTSU_RETRY_INK_FRACTION:
        block = (max(15, min(h, w) // 25)) | 1  # odd, scales with resolution
        adaptive = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, 20
        )
        adaptive_ink = cv2.bitwise_not(adaptive)
        adaptive_ink = _remove_small_specks(adaptive_ink, w * h)
        adaptive_ink = _close_small_gaps(adaptive_ink)
        if float((adaptive_ink > 0).mean()) < float((ink > 0).mean()):
            logger.info(
                "cad_trace_binarize_adaptive_retry",
                otsu_ink_fraction=round(float((ink > 0).mean()), 4),
                adaptive_ink_fraction=round(float((adaptive_ink > 0).mean()), 4),
            )
            ink = adaptive_ink

    # Last resort of the cascade (B1): Sauvola local thresholding, for sheets
    # still implausibly dense after the Gaussian adaptive retry (severe
    # uneven lighting / aged paper). Picked only when it is both cleaner and
    # non-empty — the cascade must never turn a readable sheet into a blank.
    frac = float((ink > 0).mean())
    if frac > _SAUVOLA_RETRY_INK_FRACTION and hasattr(cv2, "ximgproc"):
        try:
            block = (max(15, min(h, w) // 25)) | 1
            sauvola = cv2.ximgproc.niBlackThreshold(
                gray, 255, cv2.THRESH_BINARY, block, 0.2,
                binarizationMethod=cv2.ximgproc.BINARIZATION_SAUVOLA,
            )
            sauvola_ink = cv2.bitwise_not(sauvola)
            sauvola_ink = _remove_small_specks(sauvola_ink, w * h)
            sauvola_ink = _close_small_gaps(sauvola_ink)
            s_frac = float((sauvola_ink > 0).mean())
            if 0.0 < s_frac < frac:
                logger.info(
                    "cad_trace_binarize_sauvola_retry",
                    prev_ink_fraction=round(frac, 4),
                    sauvola_ink_fraction=round(s_frac, 4),
                )
                ink = sauvola_ink
        except Exception as exc:  # noqa: BLE001 — cascade stage is best-effort
            logger.warning("cad_trace_binarize_sauvola_failed", error=str(exc)[:120])

    return ink, w, h


def _detect_sheet_frame_quad(ink, w: int, h: int):
    """Find the dominant near-full-page rectangle contour (the ГОСТ 2.301
    sheet frame) and return its 4 approximated corner points (cv2
    approxPolyDP's Nx1x2 int array, N==4), or None when no plausible frame
    is present."""
    import cv2

    contours, _ = cv2.findContours(ink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_area = -1.0
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < _FRAME_MIN_AREA_FRACTION * w * h:
            continue
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
        if len(approx) != 4:
            continue
        if area > best_area:
            best_area = area
            best = approx
    return best


def _scale_from_quad(
    quad,
    w: int,
    h: int,
    confirmed_format: str | None = None,
) -> tuple[float | None, str | None]:
    """Derive mm-per-px only from a user-confirmed ГОСТ sheet format.

    Every A-series sheet has the same aspect ratio. Image pixels therefore
    cannot distinguish A4 from A0; the former implementation always matched
    the first dict entry (A4) and silently scaled A3/A2/A1/A0 incorrectly.
    """
    import cv2

    if confirmed_format not in _GOST_SHEETS:
        return None, None
    _x, _y, fw, fh = cv2.boundingRect(quad)
    expected_w, expected_h = _frame_dimensions_mm(
        confirmed_format, landscape=fw >= fh
    )
    frame_aspect = fw / max(fh, 1.0)
    expected_aspect = expected_w / expected_h
    if abs(frame_aspect - expected_aspect) / expected_aspect > _FRAME_ASPECT_TOL:
        return None, None
    # The detected rectangle is the INNER drawing frame, not the paper edge.
    # ГОСТ margins are 20 mm on the binding side and 5 mm elsewhere.
    scale = ((expected_w / max(fw, 1.0)) + (expected_h / max(fh, 1.0))) / 2
    logger.info("sheet_frame_scale_confirmed", format=confirmed_format, scale=round(scale, 5))
    return scale, confirmed_format


def _frame_dimensions_mm(sheet_format: str, *, landscape: bool) -> tuple[float, float]:
    """Physical width/height of the inner ГОСТ frame for an A-series sheet."""
    short_mm, long_mm = _GOST_SHEETS[sheet_format]
    paper_w, paper_h = (long_mm, short_mm) if landscape else (short_mm, long_mm)
    return (
        paper_w - _FRAME_LEFT_MARGIN_MM - _FRAME_OTHER_MARGIN_MM,
        paper_h - 2 * _FRAME_OTHER_MARGIN_MM,
    )


def _pdf_page_to_png(pdf_bytes: bytes, page_index: int = 0, dpi: int = 300) -> bytes:
    """Render one PDF page into the same PNG contract as raster uploads."""
    import fitz

    if dpi < 72 or dpi > 600:
        raise ValueError("PDF DPI должен быть в диапазоне 72..600")
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if not 0 <= page_index < document.page_count:
            raise ValueError(
                f"Страница PDF {page_index + 1} отсутствует; всего страниц: {document.page_count}"
            )
        pixmap = document[page_index].get_pixmap(
            matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
            alpha=False,
        )
        return pixmap.tobytes("png")


def _frame_segments_from_quad(quad):
    """Synthesize the 4 sides of a detected sheet frame as Segment entities.

    The skeleton-tracing recognizer (drawing_vectorize) reliably finds the
    frame's ink but routinely FRAGMENTS it into dozens of short polylines —
    ГОСТ frames carry small perpendicular tick marks (zone/fold references)
    along their length, and each one is a junction that splits the
    continuous border into a new piece. Confirmed live (2026-07-11): on a
    clean, perfectly digital source drawing (detal_126.png — the easiest
    possible case) this fragmentation alone accounted for most of a 37%
    coverage-recall shortfall, because a rectangle's 4 long straight sides
    are exactly the geometry precision won't tolerate re-deriving via noisy
    fragment-stitching. The quad is already known deterministically from
    contour detection (same one _scale_from_quad uses) — emitting it
    directly is strictly more reliable than reassembling it from skeleton
    fragments, at the cost of some duplicate short polylines already found
    by CV along the same border (harmless for recall/precision scoring and
    editable away later; not worth an exclusion-zone plumbing change to
    avoid for revision 0).
    """
    from app.ai.cad_ir.schema import Point, Segment

    pts = [(float(p[0][0]), float(p[0][1])) for p in quad]
    return [
        Segment(
            p1=Point(x=pts[i][0], y=pts[i][1]),
            p2=Point(x=pts[(i + 1) % 4][0], y=pts[(i + 1) % 4][1]),
            line_class="contour",
            width_class="main",
            confidence=0.9,
            origin="cv",
        )
        for i in range(4)
    ]


def _detect_title_block(ink, w: int, h: int) -> dict | None:
    """Bottom-right heuristic (ГОСТ 2.104 основная надпись). Conservative:
    only reports a detection when that corner actually carries meaningful
    ink — an essentially blank corner (no stamp at all, or one cropped out
    of the scan) reports None rather than a confident-looking empty dict."""
    x0 = int(w * (1 - _TITLE_BLOCK_WIDTH_RATIO))
    y0 = int(h * (1 - _TITLE_BLOCK_HEIGHT_RATIO))
    region = ink[y0:h, x0:w]
    if region.size == 0:
        return None
    ink_fraction = float((region > 0).mean())
    if ink_fraction < _TITLE_BLOCK_MIN_INK_FRACTION:
        return None
    return {
        "detected": True,
        "region": {"x0": x0, "y0": y0, "x1": w, "y1": h},
        "ink_fraction": round(ink_fraction, 4),
    }


# ГОСТ 2.109 stamp scale callout, e.g. "М 1:2". The "М"/"M" prefix is
# REQUIRED (not just decorative) — the stamp region also carries "Лист X
# Листов Y" and drawing-number fields that could otherwise coincidentally
# look like a bare "N:M" ratio; a wrongly-inferred scale would produce a
# false ESKD_SCALE_NONSTANDARD warning, exactly the noise this is meant to
# avoid, not add.
_STAMP_SCALE_PATTERN = re.compile(r"[MМ]\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)


def _extract_stamp_scale(text_entities: list, region: dict) -> str | None:
    """The one real producer of ``ir.sheet.title_block["scale"]`` —
    cad_validate.ESKD_SCALE_NONSTANDARD reads that field but nothing wrote
    it before this: scan OCR text that landed inside the detected stamp
    region for a "N:M" ratio callout, normalized to bare "N:M" (stripping
    the "М" prefix) to match cad_validate's expected format exactly."""
    x0, y0, x1, y1 = region["x0"], region["y0"], region["x1"], region["y1"]
    for e in text_entities:
        pos = getattr(e, "position", None)
        text = getattr(e, "text", None)
        if pos is None or not text:
            continue
        if not (x0 <= pos.x <= x1 and y0 <= pos.y <= y1):
            continue
        m = _STAMP_SCALE_PATTERN.search(text)
        if m:
            num = m.group(1).replace(",", ".")
            den = m.group(2).replace(",", ".")
            return f"{num}:{den}"
    return None


# A tesseract hit is one of three things on a dense CAD sheet:
#   1. real text   → keep as a TextEntity, and exclude its box from tracing.
#   2. geometry misread as a glyph (a vertical line as "|", a crosshair as
#      "+", a tick as "~") → NOT text; must be TRACED, not excluded, so it is
#      dropped entirely here (no box, no entity).
#   3. a text-shaped smudge tesseract can't read confidently → exclude its
#      box so its strokes aren't traced as messy lines, but DON'T ship a
#      garbage TextEntity that clutters the drawing.
# Without this split a clean sheet came back with 220+ single-char "в"/"8"/
# "|" noise entities and holes punched in real geometry (B2 review, 2026-07-13).
_TEXT_MIN_CONF_SINGLE = 70.0
_TEXT_MIN_CONF_SHORT = 60.0   # 2 chars
_TEXT_MIN_CONF_LONG = 58.0    # 3+ chars
_TEXT_MAX_ASPECT = 8.0        # thinner than this = a line, not text
_TEXT_MIN_ALNUM_RATIO = 0.6   # mostly letters/digits, not stray punctuation


def _classify_ocr_region(region, lenient: bool = False) -> str:
    """"text" (real label), "smudge" (exclude only) or "geometry" (ignore).

    ``lenient`` keeps low-confidence but plausibly text-shaped reads as text
    (for a downstream VLM re-read) instead of demoting them to smudge."""
    compact = (region.text or "").strip().replace(" ", "")
    w, h = max(region.w, 1), max(region.h, 1)
    aspect = max(w / h, h / w)
    # A very thin/elongated box or a pure-punctuation read is geometry.
    if aspect > _TEXT_MAX_ASPECT:
        return "geometry"
    if compact and all(not c.isalnum() for c in compact):
        return "geometry"
    if not compact or not any(c.isalnum() for c in compact):
        return "smudge"
    # A confident read polluted with punctuation ("en)", "c~", "0009 Д") is a
    # geometry-adjacent misread, not a clean label.
    alnum_ratio = sum(c.isalnum() for c in compact) / len(compact)
    if alnum_ratio < _TEXT_MIN_ALNUM_RATIO:
        return "smudge"
    if lenient:
        return "text"  # let the VLM stage judge the reading
    n = len(compact)
    threshold = (
        _TEXT_MIN_CONF_SINGLE if n == 1
        else _TEXT_MIN_CONF_SHORT if n == 2
        else _TEXT_MIN_CONF_LONG
    )
    return "text" if region.conf >= threshold else "smudge"


def _dewarp_photo(image_bytes: bytes) -> bytes:
    """Perspective-correct a phone photo to a straight-on sheet view.

    A raw album photo carries the desk, the spiral binding and perspective
    skew — all of which the recognizer otherwise traces as geometry (measured
    live: binding perforations and the table edge became segments). Reuse the
    cleanup path's document-scanner dewarp, which is saturation-based and only
    fires on a confident paper quad; for a clean scan/export it returns None
    and this is a no-op.
    """
    try:
        import io

        import numpy as np
        from PIL import Image

        from app.ai.drawing_cleanup import _dewarp_sheet

        arr = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        warped = _dewarp_sheet(arr)
        if warped is None:
            return image_bytes
        buffer = io.BytesIO()
        Image.fromarray(warped).save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 — dewarp is best-effort, never fatal
        return image_bytes


def _overlay_spec_annotations(ir, spec: dict) -> None:
    """Place the read annotations as ГОСТ 2.316 technical requirements.

    The drafted geometry already carries its own dimensions, so only the
    SEMANTIC layer belongs here: roughness, hardness, thread and tolerance
    notes plus the material. Dimensions are deliberately NOT repeated as prose
    — they are dimension entities on the geometry, and a duplicate text column
    would state them twice with no way to tell which one is authoritative.

    The block goes INSIDE the sheet, above the title block, per ГОСТ 2.316.
    It used to be written below ``image_height`` and then grew the canvas,
    which put it off the sheet and desynced the canvas from ``sheet.format``.
    """
    from app.ai.cad_ir.schema import Point, TextEntity
    from app.ai.cad_recognize.spec_vectorize import (
        TECHNICAL_REQUIREMENTS_COLUMN_MM,
        technical_requirements_lines,
    )

    # The SAME function the drafter used to reserve room for this block. Two
    # separate implementations would drift, and the failure mode is silent:
    # the sheet reserves one height and the annotator writes another, so the
    # requirements land back on top of the views.
    block = technical_requirements_lines(spec)
    if not block:
        return

    sheet_w = float(ir.source.image_width)
    sheet_h = float(ir.source.image_height)
    # Paper-space text height: ~5 mm at the drafter's 4 px/mm sheet canvas.
    height = 5.0 * 4.0
    px_per_mm = 4.0
    left = max(
        10.0 * px_per_mm,
        sheet_w - (TECHNICAL_REQUIREMENTS_COLUMN_MM + 20.0) * px_per_mm,
    )
    step = height * 1.6
    # Stack UPWARD from just above the stamp band: the block grows with the
    # part's requirements, and anchoring its top would push it into the views.
    bottom = sheet_h - (_TITLE_BLOCK_H_MM_PX + 10.0 * px_per_mm)
    top = bottom - step * len(block)
    if top < 0:  # more requirements than sheet — keep them on it regardless
        top = 0.0
    y = top + height
    for text in block:
        ir.entities.append(
            TextEntity(
                position=Point(x=left, y=y),
                text=text, height=height,
                line_class="dim", width_class="thin", origin="spec",
                assurance="inferred",
            )
        )
        y += step
    # The canvas is the sheet: it is NOT grown to fit annotations.


async def _derive_solid_views(candidate, report: dict) -> dict | None:
    """Orthographic views of the compiled solid, verified against it.

    Returns None when the kernel cannot project (an older kernel, an
    unprojectable shape): derived views are an enrichment of the redraw, not a
    precondition for it.
    """
    from app.ai.cad_projection import verify_views_against_solid
    from app.services.cad_kernel import project_candidate

    try:
        views = await project_candidate(candidate, views=("front", "side"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("cad_projection_failed", error=str(exc))
        return None
    if not views:
        return None
    return {
        "views": views,
        "verification": verify_views_against_solid(views, report),
    }


async def _build_spec_solid(
    spec: dict,
    generation_id: str,
    owner_sub: str | None,
    *,
    sheet_format: str | None = None,
    landscape: bool = True,
    require_source_evidence: bool = False,
    source_sha256: str | None = None,
    source_uri: str | None = None,
    engineering_graph_override: Any | None = None,
    require_envelope_match: bool = True,
) -> dict | None:
    """Compile the read spec into a solid, and draw the sheet from that solid.

    Returns a summary for ``params.solid_3d`` — plus, under ``_sheet_ir``, the
    drawing itself, which the caller pops off before storing. The solid is no
    longer an extra artifact beside a separately drafted sheet: it is where the
    sheet comes from, so nothing on paper can disagree with the part.

    Still never raises. A kernel that is down, or a part it cannot build, is
    reported as such — with what was read kept — rather than crashing the task.
    """
    from app.ai.cad_solid import (
        estimate_mass_kg,
        feature_tree_from_spec,
        solid_build_gate,
        solid_preview_gate,
        verify_solid_against_spec,
    )
    from app.ai.cad_process_log import record_cad_process_event
    from app.services.cad_kernel import (
        CadKernelError,
        candidate_compile_payload,
        compile_candidate,
    )
    from app.storage import upload_file as _upload

    await record_cad_process_event(
        "solid.normalize",
        "started",
        "Преобразование прочитанной спецификации в feature tree",
        None,
    )
    candidate = feature_tree_from_spec(spec)
    engineering_graph = engineering_graph_override
    from app.config import settings as _settings

    if engineering_graph is not None:
        from app.ai.cad_emg_compat import feature_tree_from_graph

        # Rebuilds prepare and persist their GraphPatch before entering the
        # kernel. The supplied immutable revision is therefore the only build
        # input, including when the corrected spec differs from the first read.
        candidate = feature_tree_from_graph(engineering_graph, target_id="preview")
    elif _settings.emg_enabled_for("mechanical"):
        from app.ai.cad_emg_compat import (
            feature_tree_from_graph,
            legacy_spec_as_low_assurance,
            spec_feature_tree_as_graph,
        )

        graph_id = f"image-generation:{generation_id}"
        if candidate is None:
            engineering_graph = legacy_spec_as_low_assurance(
                spec,
                graph_id=graph_id,
                source_sha256=source_sha256,
                source_uri=source_uri,
            )
        else:
            engineering_graph = spec_feature_tree_as_graph(
                spec,
                candidate,
                graph_id=graph_id,
                source_sha256=source_sha256,
                source_uri=source_uri,
            )
            # Ф2.6: the kernel never builds from the freshly-computed
            # candidate directly. The candidate is sealed into an EMG graph
            # revision first, then re-derived by compiling that same
            # revision's BuildOperation nodes — the graph, not the flat
            # spec read, is what the kernel boundary actually consumes.
            candidate = feature_tree_from_graph(
                engineering_graph,
                target_id="preview",
            )
    if candidate is None:
        await record_cad_process_event(
            "solid.normalize",
            "failed",
            "Feature tree не сформирован: недостаточно геометрии",
            None,
        )
        return {
            "built": False,
            "build_status": "blocked",
            "error": (
                "прочитанного не хватает на деталь: нужен либо ступенчатый контур "
                "с длинами, либо плоский контур с толщиной"
            ),
            "label": str(spec.get("part") or ""),
            **(
                {"_engineering_model_graph": engineering_graph}
                if engineering_graph is not None else {}
            ),
        }
    build_gate = solid_build_gate(
        spec, candidate, require_source_evidence=require_source_evidence
    )
    preview_gate = solid_preview_gate(build_gate)
    # A2: a step whose length could not be read compiles anyway, with a
    # provisional average length (ParamProvenance.origin="guessed") — real
    # geometry for the 3D editor to show and let a human fix, instead of the
    # whole candidate silently discarded. Routed through the SAME
    # preview_review_required path as the existing excluded-geometry preview
    # (CadModelViewer draft, accept-vectorize refusal) rather than a third
    # parallel state — the human must correct or explicitly re-affirm the
    # guess (feature_spec_path correction + rebuild) before release.
    has_unconfirmed_guess = any(
        provenance.origin == "guessed"
        for feature in candidate.features
        for provenance in feature.param_provenance.values()
    )
    preview_mode = (
        (not bool(build_gate["allowed"]) and bool(preview_gate["allowed"]))
        or has_unconfirmed_guess
    )
    confirm_assumptions = bool(build_gate["warnings"]) or preview_mode
    kernel_input = candidate_compile_payload(
        candidate,
        confirm_assumptions=confirm_assumptions,
        metadata={
            "generation_id": generation_id,
            "source": "spec_reader",
            "preview_review_required": preview_mode,
            "excluded_geometry": (
                " | ".join(preview_gate["excluded"]) if preview_mode else ""
            ),
        },
    )
    await record_cad_process_event(
        "solid.gate",
        "completed" if build_gate["allowed"] else "failed",
        (
            "Feature tree допущен к CAD-ядру"
            if build_gate["allowed"]
            else "Feature tree заблокирован до CAD-ядра"
        ),
        {
            "label": candidate.label,
            "blockers": build_gate["blockers"],
            "warnings": build_gate["warnings"],
            "preview_gate": preview_gate,
            "kernel_payload_sha256": kernel_input.get("sha256"),
            "confirm_assumptions": confirm_assumptions,
        },
    )
    if not build_gate["allowed"] and not preview_mode:
        await record_cad_process_event(
            "kernel.compile",
            "skipped",
            "CAD-ядро не вызвано из-за блокеров чтения",
            {"blockers": build_gate["blockers"]},
        )
        return {
            "built": False,
            "build_status": "blocked",
            "error": "критические данные чертежа не подтверждены",
            "blockers": build_gate["blockers"],
            "warnings": build_gate["warnings"],
            "preview_gate": preview_gate,
            "label": candidate.label,
            "feature_tree": candidate.model_dump(mode="json"),
            "kernel_input": kernel_input,
            **(
                {"_engineering_model_graph": engineering_graph}
                if engineering_graph is not None else {}
            ),
        }
    await record_cad_process_event(
        "kernel.compile",
        "started",
        (
            "Доказанная часть feature tree передана в CAD-ядро как проверочный черновик"
            if preview_mode
            else "Точный payload передан в CAD-ядро"
        ),
        {
            "kernel_payload_sha256": kernel_input.get("sha256"),
            "candidate_kind": candidate.features[0].kind if candidate.features else None,
        },
    )
    try:
        artifacts = await compile_candidate(
            candidate,
            # Critical uncertainty has already been refused above.  Remaining
            # items are explicit review warnings (for example a drawing that
            # contains no section and may legitimately describe a solid shaft).
            confirm_assumptions=confirm_assumptions,
            metadata={
                "generation_id": generation_id,
                "source": "spec_reader",
                "preview_review_required": preview_mode,
                "excluded_geometry": (
                    " | ".join(preview_gate["excluded"]) if preview_mode else ""
                ),
            },
        )
    except CadKernelError as exc:
        logger.warning("cad_solid_failed", generation_id=generation_id, error=str(exc))
        await record_cad_process_event(
            "kernel.compile", "failed", "CAD-ядро отклонило feature tree",
            {"error": str(exc)[:400]},
        )
        return {
            "built": False,
            "build_status": "blocked",
            "error": str(exc)[:400],
            "label": candidate.label,
            **(
                {"_engineering_model_graph": engineering_graph}
                if engineering_graph is not None else {}
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("cad_solid_error", generation_id=generation_id, error=str(exc))
        await record_cad_process_event(
            "kernel.compile", "failed", "Ошибка вызова CAD-ядра",
            {"error": f"{type(exc).__name__}: {exc}"[:400]},
        )
        return {
            "built": False,
            "build_status": "blocked",
            "error": str(exc)[:400],
            "label": candidate.label,
            **(
                {"_engineering_model_graph": engineering_graph}
                if engineering_graph is not None else {}
            ),
        }

    report = artifacts.report or {}
    await record_cad_process_event(
        "kernel.compile",
        "completed",
        "CAD-ядро вернуло B-Rep и отчёт операций",
        {
            "volume_mm3": report.get("volume_mm3"),
            "bounds_mm": report.get("bounds_mm"),
            "feature_operations": len(report.get("feature_operations") or []),
            "artifacts": {
                "step": bool(artifacts.step),
                "iges": bool(artifacts.iges),
                "stl": bool(artifacts.stl),
            },
        },
    )
    verification = verify_solid_against_spec(
        report, spec, candidate, require_envelope_match=require_envelope_match
    )
    await record_cad_process_event(
        "kernel.verify",
        "completed" if verification.ok else "failed",
        (
            (
                "B-Rep проверочного черновика прошёл проверку только включённых операций; "
                "исключённая геометрия остаётся блокером"
                if preview_mode
                else "B-Rep прошёл проверку размеров, топологии и всех операций"
            )
            if verification.ok
            else "B-Rep отклонён: размеры, топология или операции не совпали с feature tree"
        ),
        verification.as_dict(),
    )
    if not verification.ok:
        failed = verification.as_dict().get("failed_features") or []
        return {
            "built": False,
            "build_status": "blocked",
            "error": (
                "CAD-ядро не подтвердило всю геометрию"
                + (": " + "; ".join(str(item) for item in failed[:4]) if failed else "")
            )[:400],
            "label": candidate.label,
            "feature_tree": candidate.model_dump(mode="json"),
            "kernel_input": kernel_input,
            "verification": verification.as_dict(),
            "kernel_report": {
                "bounds_mm": report.get("bounds_mm"),
                "volume_mm3": report.get("volume_mm3"),
                "feature_results": report.get("feature_results") or [],
                "warnings": report.get("warnings") or [],
            },
            **(
                {"_engineering_model_graph": engineering_graph}
                if engineering_graph is not None else {}
            ),
        }
    # The sheet itself: views, sections and dimensions all measured off this
    # solid. A kernel too old to draw returns nothing, and the caller says so
    # rather than substituting a drawing made some other way.
    from app.ai.cad_ir.sheet_from_solid import build_sheet_from_solid

    sheet: Any = None
    await record_cad_process_event(
        "sheet.build",
        "started",
        "Построение необходимых 2D-видов из B-Rep",
        {"geometry_only": True, "sheet_format": sheet_format},
    )
    try:
        sheet = await build_sheet_from_solid(
            candidate, spec, report,
            sheet_format=sheet_format, landscape=landscape, geometry_only=True,
        )
    except Exception as exc:  # noqa: BLE001 — a drawing failure is reportable, not fatal
        logger.warning(
            "cad_sheet_from_solid_failed", generation_id=generation_id, error=str(exc)
        )
        await record_cad_process_event(
            "sheet.build", "failed", "Не удалось построить лист из B-Rep",
            {"error": f"{type(exc).__name__}: {exc}"[:400]},
        )
    # Stage 3: what the sheet said is bound to the edges the kernel built,
    # addressed by its own stable keys — the pair a CAM plan needs.
    projection = await _derive_solid_views(candidate, report)
    from app.ai.cad_semantics import bind_spec_to_solid, collect_part_properties

    semantics = bind_spec_to_solid(spec, report)
    properties = collect_part_properties(spec, report)
    # Stage 5: the ЕСТД generator's own input, built from measured geometry and
    # bound callouts instead of features guessed off a raster.
    from app.ai.cad_machining import blank_from_solid, surface_specs_from_solid

    machining = {
        "surfaces": surface_specs_from_solid(semantics, properties),
        "blank": blank_from_solid(properties),
    }
    graph_token = (
        f"r{engineering_graph.revision}-{engineering_graph.canonical_sha256[:16]}"
        if engineering_graph is not None
        else "unsealed"
    )
    prefix = (
        f"image-gen/{owner_sub or 'shared'}/{generation_id}_solid_{graph_token}"
    )
    paths: dict[str, str] = {}
    topology_bytes = None
    if artifacts.topology:
        import json as _json

        # Ф3.1/3.2: per-face tessellation for the interactive viewer's
        # raycasting — same optional, best-effort treatment as IGES.
        topology_bytes = _json.dumps(artifacts.topology, ensure_ascii=False).encode("utf-8")
    for suffix, extension, payload, content_type in (
        ("step", "step", artifacts.step, "application/step"),
        ("iges", "iges", artifacts.iges, "application/iges"),
        ("stl", "stl", artifacts.stl, "model/stl"),
        ("topology", "topology.json", topology_bytes, "application/json"),
    ):
        if not payload:
            continue
        path = f"{prefix}.{extension}"
        _upload(payload, path, content_type)
        paths[suffix] = path

    material = str((spec.get("title_block") or {}).get("material") or "")
    result = {
        "built": True,
        # Geometry-vs-spec is necessary but not sufficient: the spec itself was
        # read by a model.  Until an independent source-projection comparison
        # passes, a compiled body is explicitly unverified.
        "build_status": (
            "preview_review_required" if preview_mode else "built_unverified"
        ),
        "complete": not preview_mode,
        "preview": preview_mode,
        "blockers": build_gate["blockers"],
        "excluded_geometry": preview_gate["excluded"] if preview_mode else [],
        "label": candidate.label,
        "paths": paths,
        "assumptions": candidate.missing_data,
        "build_gate": build_gate,
        "preview_gate": preview_gate,
        "verification": {
            **verification.as_dict(),
            "feature_complete": not preview_mode,
            "preview_only": preview_mode,
        },
        "source_projection_verification": {
            "ok": False,
            "status": "not_run",
            "reason": "независимое сравнение проекций с исходным чертежом не выполнено",
        },
        "volume_mm3": report.get("volume_mm3"),
        "surface_area_mm2": report.get("surface_area_mm2"),
        "bounds_mm": report.get("bounds_mm"),
        "kernel_report": {
            "bounds_mm": report.get("bounds_mm"),
            "volume_mm3": report.get("volume_mm3"),
            "surface_area_mm2": report.get("surface_area_mm2"),
            "feature_operations": report.get("feature_operations") or [],
            "warnings": report.get("warnings") or [],
            # Ф3 нового CAD-редактора: edge_key candidates for a
            # fillet/chamfer form (_edge_descriptors — the SAME keys
            # _resolve_edge already accepts on the next add-feature call).
            "edges": report.get("edges") or [],
        },
        "mass_kg": estimate_mass_kg(report.get("volume_mm3"), material),
        "projection": projection,
        "semantics": semantics,
        "properties": properties,
        "machining": machining,
        # The feature tree travels with the result so the editor can offer the
        # part AS READ for parametric editing. Without it the 3D panel proposed
        # candidates re-derived from 2D heuristics and the tree actually built
        # never reached the person editing it.
        "feature_tree": candidate.model_dump(mode="json"),
        "kernel_input": kernel_input,
        **(
            {"_engineering_model_graph": engineering_graph}
            if engineering_graph is not None else {}
        ),
    }
    if sheet is not None:
        result["sheet"] = {
            "part_class": sheet.plan.part_class,
            "views": [view["kind"] for view in sheet.plan.views],
            "view_reasons": sheet.plan.view_reasons,
            "scale": sheet.plan.scale_label,
            "sheet_format": sheet.plan.sheet_format,
            "geometry_only": sheet.plan.geometry_only,
            "dimensions": len(sheet.drawing.get("dimensions") or []),
            "verification": sheet.verification,
            "warnings": sheet.warnings,
        }
        # Popped by the caller: an IR is not JSON for gen.params.
        result["_sheet_ir"] = sheet.ir
        result["_dimensions"] = sheet.drawing.get("dimensions") or []
        await record_cad_process_event(
            "sheet.build",
            "completed",
            "2D-лист построен из проверяемой 3D-модели",
            {
                "views": [view["kind"] for view in sheet.plan.views],
                "dimensions": len(sheet.drawing.get("dimensions") or []),
                "geometry_only": sheet.plan.geometry_only,
                "warnings": sheet.warnings,
            },
        )
    return result


def _revalidated_spec(spec: dict) -> dict:
    """Re-derive ``unresolved`` after a follow-up filled a value in.

    The contract's own validator APPENDS its machine codes, so a length that a
    second look just recovered would keep its old "length-missing" entry and go
    on blocking the build. Those generated codes are dropped and recomputed;
    free-text reasons (a consensus disagreement, a refused bore) are kept —
    nothing about them changed.
    """
    from app.ai.cad_recognize.spec_vectorize import (
        EngineeringDrawingSpec,
        assign_stable_feature_ids,
    )

    candidate = dict(spec)
    candidate["unresolved"] = [
        item for item in (spec.get("unresolved") or [])
        if not str(item).startswith("body:") and not str(item).startswith("view:")
    ]
    try:
        revalidated = EngineeringDrawingSpec.model_validate(candidate).model_dump(
            mode="json"
        )
    except Exception:  # noqa: BLE001 — keep the read rather than lose it to a re-check
        return spec
    # Fields the contract does not know about (provenance, consensus telemetry,
    # fragment stats) are dropped by validation — carry them across.
    for key, value in spec.items():
        if key not in revalidated:
            revalidated[key] = value
    # Idempotent: a rebuild/correction path can reach here without ever having
    # gone through read_spec_best_effort's own assignment — every feature
    # still needs a stable id for SpecView.features_shown / the native EMG
    # graph to reference, and re-running this never reassigns an existing one.
    return assign_stable_feature_ids(revalidated)


async def _store_failed_reading(
    factory,
    gen_uuid,
    spec: dict,
    crosscheck: dict,
    followup_log: list,
    unresolved: list[str],
    solid_result: dict | None,
    pipeline_manifest: dict,
    *,
    build_note: str | None = None,
    fallback_ir: Any | None = None,
    owner_sub: str | None = None,
) -> bool:
    """Keep what was read when the part could not be built.

    Reading the sheet is the expensive, fallible half; building from a complete
    reading is the cheap, deterministic one. Throwing the reading away because
    the second half failed means paying for it again — and the reading is often
    nearly right, one missing length short of a part. Stored here, the editor
    can show it, a person can complete it, and the rebuild costs nothing.
    A part that could not be built is not a failure of the run — it is a
    review-required draft: the generation lands as ``done`` (mirroring the
    post-build unresolved-dimension path below), never ``failed``. Landing
    here as ``failed`` made the saved reading practically undiscoverable
    (buried behind a red error banner, no CadIrRevision for the editor to
    open) even though it was captured specifically so a person could finish
    it and rebuild.

    ``fallback_ir`` — when the 3D solid did not build but the deterministic or
    generative Model-2 drafter still managed a 2D sheet from the same spec —
    is persisted as a normal CadIrRevision, so the editor opens on real,
    patchable geometry instead of a text-only params dump. Returns whether a
    revision was actually created.
    """
    from app.ai.cad_dimension_graph import build_dimension_graph
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.services import studio_queue

    revision_created = False
    try:
        async with factory() as db:
            gen = await db.get(ImageGeneration, gen_uuid)
            if gen is None:
                return False
            stored_solid = dict(solid_result or {"built": False})
            engineering_graph = stored_solid.pop("_engineering_model_graph", None)
            graph_ref = None
            if engineering_graph is not None:
                from app.services.engineering_model_graph import persist_pipeline_graph

                graph_row = await persist_pipeline_graph(db, engineering_graph)
                graph_ref = {
                    "revision_id": str(graph_row.id),
                    "graph_id": graph_row.graph_id,
                    "revision": graph_row.revision,
                    "canonical_sha256": graph_row.canonical_sha256,
                }
            review_warnings = list(dict.fromkeys(unresolved))
            if build_note:
                review_warnings.append(build_note)
            gen.params = {
                **(gen.params or {}),
                "vectorize_method": "spec",
                "spec": spec,
                "spec_crosscheck": crosscheck,
                "dimension_graph": build_dimension_graph(spec),
                "spec_followup": followup_log,
                "spec_review_warnings": review_warnings,
                "solid_3d": stored_solid,
                "sheet_without_geometry": fallback_ir is None,
                "digitization_status": "review_required",
                "cad_reading": {
                    "spec": spec,
                    "attempts": spec.get("reader_attempts") or [],
                    "reader_models": (
                        pipeline_manifest.get("components", {})
                        .get("spec_reader", {})
                        .get("models", [])
                    ),
                    "followup": followup_log,
                    "crosscheck": crosscheck,
                    "dimension_graph": build_dimension_graph(spec),
                    "unresolved": list(dict.fromkeys(unresolved)),
                },
                "solid_input": (solid_result or {}).get("kernel_input"),
                "cad_pipeline_manifest": pipeline_manifest,
                **(
                    {"engineering_model_graph": graph_ref}
                    if graph_ref is not None else {}
                ),
            }
            if fallback_ir is not None:
                from app.ai.cad_ir.schema import ValidationIssueIR
                from app.services import cad_ir_store

                fallback_ir.source.generation_id = str(gen_uuid)
                fallback_ir.digitization_status = "review_required"
                if unresolved:
                    fallback_ir.validation.issues.append(
                        ValidationIssueIR(
                            code="SPEC_READER_UNRESOLVED",
                            severity="warn",
                            level=3,
                            message_ru=(
                                "Геометрия черновика требует решения пользователя: "
                                + "; ".join(dict.fromkeys(unresolved))
                            ),
                        )
                    )
                await cad_ir_store.save_revision(
                    db, gen, fallback_ir, origin="auto", created_by=owner_sub,
                    keep_raster=None, thin_px=2, thick_px=4,
                )
                revision_created = True
            gen.status = ImageGenStatus.done
            gen.error = None
            job = await studio_queue.job_for_generation(db, gen_uuid)
            await studio_queue.mark_job_done(db, job)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — the failure message matters more
        logger.warning("cad_spec_reading_not_stored", error=str(exc)[:200])
        from app.config import settings as _settings

        if _settings.emg_enabled_for("mechanical"):
            raise
    return revision_created


def _unplaced_callouts(solid_result: dict, spec: dict) -> dict:
    """Which read dimensions the drawing does NOT show.

    The old check asked whether the drafted geometry measures the numbers it was
    drafted FROM — which it cannot help but do, so it could only ever cry wolf
    or say nothing. Now that dimensions are measured off the solid, the question
    that carries information is the reverse: the sheet stated these sizes, which
    of them is missing from the drawing?

    A missing callout is not an error. A chamfer note, a roughness symbol and a
    thread designation are all read values that no linear dimension will match,
    and the answer is a review list rather than a gate.
    """
    placed = [
        float(item["value_mm"])
        for item in (solid_result.get("_dimensions") or [])
        if isinstance(item.get("value_mm"), (int, float))
    ]
    from app.ai.cad_recognize.spec_vectorize import _callout_kind

    stated: list[tuple[float, str]] = []
    for dimension in spec.get("dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        text = str(dimension.get("value") or "").strip()
        if _callout_kind(text) is None:
            continue
        value = _spec_num(text)
        if value and value > 0:
            stated.append((value, text))

    def _shown(value: float) -> bool:
        window = max(0.05, value * 0.005)
        return any(abs(value - drawn) <= window for drawn in placed)

    unplaced = sorted({text for value, text in stated if not _shown(value)})
    return {
        "status": "ok" if not unplaced else "partial",
        "read": len(stated),
        "placed": len(placed),
        "unplaced": unplaced[:16],
    }


def _spec_num(text: str) -> float | None:
    from app.ai.cad_recognize.spec_vectorize import _num

    return _num(text)


def _verify_spec_dimensions(ir, spec: dict) -> dict:
    """Advisory reuse of the graph verifier's dimension-vs-geometry idea.

    A from-spec redraw builds its geometry FROM the stated numbers, so this is
    a guard against drafter bugs (a dropped section, a wrong scale) rather than
    a pixel gate: for every dimension the spec stated, does SOME drafted feature
    actually measure that value? Kinds are pooled together deliberately — a
    shaft diameter renders as a vertical step segment, not a circle, so keying
    the check on entity type would cry wolf. Non-blocking; surfaced for review.
    """
    import math

    from app.ai.cad_ir.schema import Arc, Circle, Segment

    scale = ir.scale
    if not scale or scale <= 0:
        return {"status": "scale_unknown", "checked": 0, "matched": 0, "unmatched": []}

    measured: list[float] = []
    for entity in ir.entities:
        if isinstance(entity, Segment):
            measured.append(
                math.hypot(entity.p2.x - entity.p1.x, entity.p2.y - entity.p1.y) * scale
            )
        elif isinstance(entity, (Circle, Arc)):
            measured.append(2.0 * entity.radius * scale)

    stated: list[float] = []
    for body in [spec.get("main_view"), *(spec.get("parts") or [])]:
        if not isinstance(body, dict):
            continue
        for group in ("outer", "bore"):
            for section in body.get(group) or []:
                for key in ("diameter_mm", "length_mm"):
                    value = section.get(key) if isinstance(section, dict) else None
                    if isinstance(value, (int, float)) and value > 0:
                        stated.append(float(value))
        profile = body.get("profile")
        if isinstance(profile, dict):
            for key in ("width_mm", "height_mm", "diameter_mm", "thickness_mm"):
                value = profile.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    stated.append(float(value))

    def _match(value: float) -> bool:
        # 0.5% — the same window the graph verifier uses. The drafter builds
        # geometry FROM these numbers, so anything looser would hide a real
        # drafter bug (a dropped section, a wrong scale) instead of catching it.
        tol = max(0.05, abs(value) * 0.005)
        return any(abs(m - value) <= tol for m in measured)

    unique = sorted({round(value, 2) for value in stated})
    unmatched = [value for value in unique if not _match(value)]
    return {
        "status": "ok" if not unmatched else "mismatch",
        "checked": len(unique),
        "matched": len(unique) - len(unmatched),
        "unmatched": [f"{value:g} мм" for value in unmatched[:12]],
    }


def _drop_in_glyph_segments(entities: list, text_entities: list) -> list:
    """Remove glyph-stroke segments that lie inside a text box.

    Text is deliberately not pre-excluded from tracing (blanking text boxes
    deletes the geometry the dimension text sits on), so glyph strokes arrive
    as tiny segments and are dropped here. Two guards keep this from ever
    eating real geometry when a text box is wrong:

    * a box far taller than the sheet's typical text height is a mis-snapped
      label that swallowed geometry — it is ignored, never used to delete; and
    * only a stroke SHORTER than the box's own text height is removed, so a
      long line (a shaft body edge, a dimension line) that merely passes
      through a label survives even when the box is oversized.
    """
    import math

    boxes = [
        t.source_region for t in text_entities if getattr(t, "source_region", None)
    ]
    if not boxes:
        return list(entities)
    heights = sorted(box.y1 - box.y0 for box in boxes)
    median_h = heights[len(heights) // 2] if heights else 0.0
    usable = [
        box for box in boxes if median_h <= 0 or (box.y1 - box.y0) <= 3.0 * median_h
    ]

    def _is_glyph_stroke(seg) -> bool:
        length = math.hypot(seg.p2.x - seg.p1.x, seg.p2.y - seg.p1.y)
        for box in usable:
            if (
                box.x0 - 1.0 <= seg.p1.x <= box.x1 + 1.0
                and box.y0 - 1.0 <= seg.p1.y <= box.y1 + 1.0
                and box.x0 - 1.0 <= seg.p2.x <= box.x1 + 1.0
                and box.y0 - 1.0 <= seg.p2.y <= box.y1 + 1.0
                and length <= 1.5 * (box.y1 - box.y0)
            ):
                return True
        return False

    return [
        e for e in entities if not (e.type == "segment" and _is_glyph_stroke(e))
    ]


def _ocr_text_entities(image_bytes: bytes, lenient: bool = False):
    """OCR → (TextEntity list, exclusion boxes for the recognizer). Only
    confident, text-shaped reads become entities; geometry misread as glyphs
    is left for the recognizer to trace; unreadable smudges are excluded from
    tracing but never shipped as garbage text. ``lenient`` keeps low-conf
    text-shaped reads (VLM enrichment will re-read them)."""
    from app.ai.cad_ir.schema import Point, SourceRegion, TextEntity
    from app.ai.text_preserve import detect_text_regions

    # glyphs_only: this caller ships region.text as a TextEntity, so the
    # string has to be right, and on a full technical sheet tesseract's
    # layout analysis competes with the linework and loses.
    classified = [
        (region, _classify_ocr_region(region, lenient=lenient))
        for region in detect_text_regions(image_bytes, glyphs_only=True)
    ]
    # Height sanity (2026-07-17, user report "огромный текст"): tesseract
    # sometimes merges hatching/dimension strokes into one tall region, and
    # rendering that box height verbatim paints giant labels over the
    # drawing. Judge every text box against the sheet's own median text
    # height: an outlier ≥2.5× the median is a merged region → demote to
    # smudge (excluded, not drawn); survivors get their render height clamped
    # to 2× the median. ЕСКД text on one sheet simply doesn't vary 3×.
    text_heights = sorted(r.h for r, kind in classified if kind == "text")
    median_h = text_heights[len(text_heights) // 2] if text_heights else 0

    entities = []
    boxes: list[tuple[int, int, int, int]] = []
    for region, kind in classified:
        if kind == "geometry":
            continue  # trace it as linework, don't exclude
        boxes.append((region.x, region.y, region.x + region.w, region.y + region.h))
        if kind != "text":
            continue  # exclude the box, but ship no garbage TextEntity
        if median_h and region.h >= 2.5 * median_h and len(text_heights) >= 5:
            continue  # merged-region outlier: keep the exclusion, drop the label
        height = float(max(region.h, 4))
        if median_h:
            height = min(height, 2.0 * median_h)
        entities.append(
            TextEntity(
                position=Point(x=float(region.x), y=float(region.y + region.h)),
                text=region.text.strip(),
                height=height,
                confidence=max(0.0, min(1.0, region.conf / 100.0)),
                origin="cv",
                source_region=SourceRegion(
                    x0=float(region.x),
                    y0=float(region.y),
                    x1=float(region.x + region.w),
                    y1=float(region.y + region.h),
                ),
                evidence=[f"ocr:conf={region.conf:.0f}"],
            )
        )
    return entities, boxes


_VLM_ENRICH_CONFIDENCE_THRESHOLD = 0.75


async def _enrich_text_with_vlm(text_entities: list, source_bytes: bytes) -> None:
    """Escalate low-confidence OCR text to a VLM crop read (Ф4.1), attaching
    ranked alternatives in place. Bounded by MAX_CROP_READS_PER_RUN; a VLM
    failure on one crop never aborts the batch (read_crop_hypotheses already
    degrades to []) or the pipeline."""
    from app.ai.cad_hypothesis import apply_vlm_readings
    from app.ai.vlm_dimensions import MAX_CROP_READS_PER_RUN, crop_bytes_for_region, read_crop_hypotheses

    candidates = [
        e for e in text_entities
        if e.confidence < _VLM_ENRICH_CONFIDENCE_THRESHOLD and e.source_region is not None
    ][:MAX_CROP_READS_PER_RUN]
    for entity in candidates:
        crop = crop_bytes_for_region(source_bytes, entity.source_region)
        if crop is None:
            continue
        readings = await read_crop_hypotheses(crop, confidential=True)
        if readings:
            apply_vlm_readings(entity, readings)


_VLM_LINE_BUDGET = 15


async def _enrich_lines_with_vlm(ir, source_bytes: bytes) -> None:
    """Escalate ambiguous thin-stroke Segments to a VLM line classification
    (Ф4.3), attaching geometric alternatives in place. Bounded budget; one
    failed crop never aborts the rest."""
    from app.ai.cad_hypothesis import apply_line_hypotheses
    from app.ai.cad_ir.schema import Segment
    from app.ai.vlm_dimensions import classify_line_hypotheses, crop_bytes_for_bbox

    candidates = [
        e for e in ir.entities
        if isinstance(e, Segment) and e.width_class == "thin" and e.assurance != "human_approved"
    ][:_VLM_LINE_BUDGET]
    for entity in candidates:
        x0, y0 = min(entity.p1.x, entity.p2.x), min(entity.p1.y, entity.p2.y)
        x1, y1 = max(entity.p1.x, entity.p2.x), max(entity.p1.y, entity.p2.y)
        crop = crop_bytes_for_bbox(source_bytes, x0, y0, x1, y1)
        if crop is None:
            continue
        result = await classify_line_hypotheses(crop, confidential=True)
        if result.get("line_readings"):
            apply_line_hypotheses(entity, result)


def _assess_export_fidelity(ir, ink, keep_raster, thin_px: int, thick_px: int) -> None:
    """Measure only geometry that will actually reach DXF.

    Text source boxes represented by structured text/dimension entities are
    evaluated semantically by confidence/review rules, not by font-pixel
    identity. Every other source-ink component missed by the vector render
    becomes an explicit unresolved region and blocks exactness.
    """
    import io

    import cv2
    import ezdxf
    import numpy as np

    from app.ai.cad_ir.dxf_render import render_ir_to_dxf
    from app.ai.cad_ir.png_render import rasterize_entities
    from app.ai.cad_ir.schema import SourceRegion, UnresolvedRegion
    from app.ai.cad_recognize.verify import score_coverage
    from app.ai.drawing_vectorize import _coverage_dilate_px

    ink_bool = np.asarray(ink) > 0
    h, w = ink_bool.shape[:2]
    geometry_ink = ink_bool.copy()
    vector_entities = []
    for entity in ir.entities:
        if entity.type in ("text", "annotation"):
            region = entity.source_region
            if region is not None:
                x0, y0 = max(0, int(region.x0)), max(0, int(region.y0))
                x1, y1 = min(w, int(region.x1)), min(h, int(region.y1))
                geometry_ink[y0:y1, x0:x1] = False
            continue
        vector_entities.append(entity)

    score = score_coverage(
        vector_entities,
        geometry_ink,
        keep_raster=None,
        thin_px=thin_px,
        thick_px=thick_px,
    )
    ir.validation.coverage_recall = score.recall
    ir.validation.coverage_precision = score.precision
    ir.validation.vector_recall = score.recall
    ir.validation.vector_precision = score.precision
    ir.validation.raster_passthrough_fraction = (
        round(
            float((ink_bool & np.asarray(keep_raster).astype(bool)).sum())
            / max(int(ink_bool.sum()), 1),
            4,
        )
        if keep_raster is not None
        else 0.0
    )

    drawn = rasterize_entities(vector_entities, w, h, thin_px, thick_px) < 128
    radius = _coverage_dilate_px(h, w)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)
    )
    drawn_grown = cv2.dilate(drawn.astype(np.uint8), kernel) > 0
    missed = (geometry_ink & ~drawn_grown).astype(np.uint8)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(missed, connectivity=8)
    min_pixels = max(8, round(w * h * 0.000002))
    unresolved = []
    for index in range(1, count):
        x, y, width, height, area = [int(v) for v in stats[index]]
        if area < min_pixels:
            continue
        unresolved.append(
            UnresolvedRegion(
                region=SourceRegion(
                    x0=float(x), y0=float(y), x1=float(x + width), y1=float(y + height)
                ),
                reason="unvectorized_ink",
                ink_pixels=area,
            )
        )
    ir.unresolved_regions = unresolved

    try:
        data = render_ir_to_dxf(ir)
        ezdxf.read(io.StringIO(data.decode("utf-8")))
        ir.validation.dxf_reopens = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("cad_trace_dxf_reopen_failed", error=str(exc)[:160])
        ir.validation.dxf_reopens = False


async def _run(generation_id: str, task_id: str | None) -> dict:
    from app.ai.cad_ir import CadIR, SourceInfo
    from app.ai.cad_ir.schema import SheetInfo
    from app.ai.cad_recognize import CvRecognizer
    from app.ai.cad_recognize.dimensions import reconstruct_dimensions
    from app.ai.cad_recognize.technical_vectorizer import TechnicalVectorizerRecognizer
    from app.ai.cad_recognize.verify import apply_to_ir, arbitrate_recognition, score_coverage
    from app.ai.cad_validate import validate_ir
    from app.db.models import ImageGeneration, ImageGenStatus
    from app.db.session import _get_session_factory
    from app.services import cad_ir_store, studio_queue
    from app.storage import download_file, upload_file

    factory = _get_session_factory()
    gen_uuid = uuid.UUID(generation_id)

    async with factory() as db:
        gen = await db.get(ImageGeneration, gen_uuid)
        if not gen:
            return {"error": "generation not found"}
        job = await studio_queue.job_for_generation(db, gen_uuid)
        if gen.status == ImageGenStatus.cancelled:
            return {"cancelled": True}
        gen.status = ImageGenStatus.running
        gen.celery_task_id = task_id
        await studio_queue.mark_job_running(db, job, task_id=task_id)
        await db.commit()
        owner_sub = gen.owner_sub
        description = (gen.prompt or "").strip()
        params = dict(gen.params or {})
        source_paths = list(gen.source_image_paths or [])
        # Ancestry for pixel provenance: when the source is a previous
        # diffusion result, remember what THAT generation was made from.
        parent_operation: str | None = None
        parent_source_path: str | None = None
        if params.get("source_generation_id"):
            try:
                parent = await db.get(
                    ImageGeneration, uuid.UUID(str(params["source_generation_id"]))
                )
                if parent:
                    parent_operation = parent.operation
                    parent_source_path = (parent.source_image_paths or [None])[0]
            except Exception as _exc:  # noqa: BLE001 — best-effort ancestry
                logger.warning("parent_lookup_failed", error=str(_exc))

    from app.ai.cad_process_log import (
        install_cad_process_recorder,
        reset_cad_process_recorder,
    )

    async def _record(
        stage: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        await _append_cad_process_event(
            gen_uuid, stage, status, message, details
        )

    recorder_token = install_cad_process_recorder(_record)

    async def _fail(message: str) -> dict:
        from app.tasks.image_generation import _mark_failed

        await _record("pipeline", "failed", message, {"terminal": True})
        await _mark_failed(gen_uuid, message, owner_sub)
        return {"error": message}

    try:
        import hashlib

        from app.ai.cad_digitization_type import resolve_digitization_type

        vectorize_method = str(params.get("vectorize_method") or "trace")
        digitization_type = resolve_digitization_type(
            str(params.get("digitization_type") or "auto")
        )
        requested_profile = (
            digitization_type.profile
            if digitization_type.explicit
            else str(params.get("digitization_profile") or "auto")
        )
        await _record(
            "pipeline",
            "started",
            "Оцифровка запущена",
            {
                "task_id": task_id,
                "method": vectorize_method,
                "digitization_type": digitization_type.normalized,
                "domain_profile": requested_profile,
                "read_passes": int(params.get("read_passes") or 3),
            },
        )
        description_mode = vectorize_method == "text_spec" and bool(description)
        if not source_paths and not description_mode:
            return await _fail("Для оцифровки нужен исходный скан/фото.")
        content = download_file(source_paths[0]) if source_paths else b""
        source_sha256 = hashlib.sha256(
            content if content else description.encode("utf-8")
        ).hexdigest()
        await _record(
            "source.load",
            "completed",
            "Исходник загружен",
            {
                "bytes": len(content),
                "sha256": source_sha256,
                "kind": "description" if description_mode else "image",
            },
        )
        if content.startswith(b"%PDF"):
            try:
                content = _pdf_page_to_png(
                    content,
                    page_index=int(params.get("pdf_page", 0)),
                    dpi=int(params.get("pdf_dpi", 300)),
                )
            except Exception as exc:  # noqa: BLE001
                return await _fail(f"Не удалось подготовить страницу PDF: {exc}")

        # Stage 0.9: dewarp a phone photo to a straight-on sheet view, dropping
        # the desk/binding background before anything is traced. No-op for a
        # clean scan (no confident paper quad).
        if content:
            content = _dewarp_photo(content)
            await _record(
                "source.normalize",
                "completed",
                "Нормализация и dewarp исходника завершены",
                {"bytes": len(content)},
            )
        normalized_source_path = None
        normalized_source_sha256 = source_sha256
        if content:
            normalized_source_path = (
                f"image-gen/{owner_sub or 'shared'}/{generation_id}_normalized.png"
            )
            normalized_source_sha256 = hashlib.sha256(content).hexdigest()
            upload_file(content, normalized_source_path, "image/png")
        from app.ai.cad_pipeline_manifest import build_cad_pipeline_manifest

        pipeline_manifest = build_cad_pipeline_manifest(
            profile=requested_profile,
            method=str(params.get("vectorize_method") or "trace"),
            source_sha256=source_sha256,
            input_kind="description" if description_mode else "source_image",
            digitization_type=digitization_type.normalized,
        )
        # Persist the exact component/model snapshot before inference starts so
        # failed and interrupted attempts remain reproducible in the UI too.
        async with factory() as db:
            manifest_gen = await db.get(ImageGeneration, gen_uuid)
            if manifest_gen:
                manifest_gen.params = {
                    **(manifest_gen.params or {}),
                    "cad_pipeline_manifest": pipeline_manifest,
                    **(
                        {
                            "normalized_source_path": normalized_source_path,
                            "normalized_source_sha256": normalized_source_sha256,
                        }
                        if normalized_source_path
                        else {}
                    ),
                }
                await db.commit()
        await _record(
            "pipeline.manifest",
            "completed",
            "Снимок маршрута и версий компонентов сохранён",
            {
                "config_sha256": pipeline_manifest.get("config_sha256"),
                "spec_reader": (
                    pipeline_manifest.get("components", {}).get("spec_reader", {})
                ),
            },
        )

        # Three non-trace drafting workflows, kept explicitly separate:
        #   "spec"      — the production «По описанию» path: Model 1 (VLM)
        #                 READS the source image into a structured spec,
        #                 Model 2 (deterministic-first) DRAFTS clean geometry.
        #                 A redraw, not a pixel copy — no image-domain gap.
        #   "graph"     — API-ONLY experiment (2026-07-25: removed from both
        #                 UIs). A whole-sheet VLM coordinate reader emits a
        #                 full EngineeringDrawingGraph, gated fail-closed by
        #                 independent verifiers. Every live run on a real sheet
        #                 has failed (see CAD_DRAWING_GRAPH_PLAN.md): universal
        #                 VLMs do not emit accurate whole-sheet coordinates.
        #                 Kept only until its view/relation contract has been
        #                 harvested into EngineeringDrawingSpec.
        #   "text_spec" — draft from a free-text ТЗ (no source image). NOT a
        #                 digitizing method; the UI offers it as its own
        #                 workflow, shown only when no sheet is attached.
        # "trace" is the auxiliary pixel path below: it is kept for classes the
        # spec drafter cannot express yet and as a verification surface, not as
        # the way to reach an exact ЕСКД redraw.
        if vectorize_method in ("spec", "graph", "text_spec"):
            if vectorize_method == "spec" and not digitization_type.spec_redraw_supported:
                return await _fail(
                    "Выбранный тип оцифровки пока не имеет валидного "
                    "параметрического 3D/BIM-генератора. Используйте "
                    "вспомогательную трассировку либо выберите механическую "
                    "деталь; запуск неподходящего генератора запрещён."
                )
            if vectorize_method == "graph":
                from app.ai.cad_drawing_graph import (
                    DrawingGraphDraftError,
                    draft_drawing_graph,
                    read_drawing_graph_staged_attempt,
                    verify_drawing_graph,
                    verify_graph_evidence_with_vlm,
                )

                graph_attempt = await read_drawing_graph_staged_attempt(content)
                graph = graph_attempt.graph
                if graph is None:
                    async with factory() as db:
                        failed_gen = await db.get(ImageGeneration, gen_uuid)
                        if failed_gen:
                            failed_gen.params = {
                                **(failed_gen.params or {}),
                                "drawing_graph_read_attempt": graph_attempt.model_dump(
                                    mode="json", exclude={"graph"}
                                ),
                            }
                            await db.commit()
                    return await _fail(
                        "Метод «по описанию»: координатный reader не вернул "
                        "полный валидный EngineeringDrawingGraph. Частичный "
                        "чертёж не создан; проверьте graph-reader и исходный лист."
                    )
                try:
                    graph_ir = draft_drawing_graph(graph)
                except DrawingGraphDraftError as exc:
                    return await _fail(
                        "Метод «по описанию»: graph drafter остановлен: " + str(exc)
                    )
                graph_ir.source.generation_id = generation_id
                graph_ink, graph_width, graph_height = _binarize(content)
                if (
                    graph_width != graph.source.image_width
                    or graph_height != graph.source.image_height
                ):
                    return await _fail(
                        "Метод «по описанию»: размер graph не совпадает с "
                        "нормализованным исходным листом."
                    )
                _assess_export_fidelity(graph_ir, graph_ink, None, 2, 4)
                vlm_evidence = await verify_graph_evidence_with_vlm(content, graph)
                graph_verification = verify_drawing_graph(
                    graph,
                    pixel_recall=graph_ir.validation.vector_recall,
                    pixel_precision=graph_ir.validation.vector_precision,
                    vlm_evidence=vlm_evidence,
                    require_vlm_evidence=True,
                )
                if graph_verification.blocking:
                    async with factory() as db:
                        blocked_gen = await db.get(ImageGeneration, gen_uuid)
                        if blocked_gen:
                            blocked_gen.params = {
                                **(blocked_gen.params or {}),
                                "drawing_graph": graph.model_dump(mode="json"),
                                "drawing_graph_read_attempt": graph_attempt.model_dump(
                                    mode="json", exclude={"graph"}
                                ),
                                "drawing_graph_sha256": graph.content_sha256(),
                                "drawing_graph_vlm_evidence": vlm_evidence.model_dump(
                                    mode="json"
                                ),
                                "drawing_graph_verification": (
                                    graph_verification.model_dump(mode="json")
                                ),
                            }
                            await db.commit()
                    return await _fail(
                        "Метод «по описанию»: graph не прошёл независимую проверку: "
                        + "; ".join(
                            issue.message for issue in graph_verification.blocking[:5]
                        )
                    )
                validate_ir(graph_ir)
                if graph_ir.validation.blocking:
                    return await _fail(
                        "Метод «по описанию»: CadIR validation заблокировала "
                        "построение: "
                        + "; ".join(
                            issue.message_ru
                            for issue in graph_ir.validation.blocking[:5]
                        )
                    )
                async with factory() as db:
                    gen = await db.get(ImageGeneration, gen_uuid)
                    if not gen or gen.status == ImageGenStatus.cancelled:
                        return {"cancelled": True}
                    normalized_path = (
                        f"image-gen/{gen.owner_sub or 'shared'}/{gen.id}_normalized.png"
                    )
                    upload_file(content, normalized_path, "image/png")
                    emg_row = None
                    emg_ref = None
                    from app.config import settings as _settings

                    requested_emg_profile = str(requested_profile or "auto")
                    if _settings.emg_enabled_for(requested_emg_profile):
                        from app.ai.cad_emg_compat import (
                            drawing_graph_as_observations,
                        )
                        from app.services.engineering_model_graph import (
                            persist_pipeline_graph,
                        )

                        emg = drawing_graph_as_observations(
                            graph,
                            graph_id=f"image-generation:{generation_id}",
                            profile=(
                                "mechanical"
                                if requested_emg_profile == "mechanical_eskd"
                                else requested_emg_profile
                            ),
                        )
                        emg_row = await persist_pipeline_graph(db, emg)
                        emg_ref = {
                            "revision_id": str(emg_row.id),
                            "graph_id": emg_row.graph_id,
                            "revision": emg_row.revision,
                            "canonical_sha256": emg_row.canonical_sha256,
                        }
                    gen.params = {
                        **(gen.params or {}),
                        "vectorize_method": "graph",
                        "description_mode": False,
                        "drawing_graph": graph.model_dump(mode="json"),
                        "drawing_graph_read_attempt": graph_attempt.model_dump(
                            mode="json", exclude={"graph", "parsed_payload"}
                        ),
                        "drawing_graph_sha256": graph.content_sha256(),
                        "drawing_graph_vlm_evidence": vlm_evidence.model_dump(
                            mode="json"
                        ),
                        "drawing_graph_verification": graph_verification.model_dump(
                            mode="json"
                        ),
                        "cad_pipeline_manifest": pipeline_manifest,
                        "normalized_source_path": normalized_path,
                        **(
                            {"engineering_model_graph": emg_ref}
                            if emg_ref is not None else {}
                        ),
                    }
                    cad_revision = await cad_ir_store.save_revision(
                        db,
                        gen,
                        graph_ir,
                        origin="auto",
                        created_by=owner_sub,
                        keep_raster=None,
                        thin_px=2,
                        thick_px=4,
                    )
                    if emg_row is not None:
                        cad_revision.engineering_graph_revision_id = emg_row.id
                    gen.status = ImageGenStatus.done
                    job = await studio_queue.job_for_generation(db, gen_uuid)
                    await studio_queue.mark_job_done(db, job)
                    await db.commit()
                return {
                    "ok": True,
                    "generation_id": generation_id,
                    "entities": len(graph_ir.entities),
                    "relations": len(graph_ir.relations),
                    "method": "drawing_graph",
                }

            # Production «По описанию»: Model 1 (VLM) reads the source IMAGE
            # into a structured spec; Model 2 (deterministic-first) drafts
            # clean parametric geometry from it. Understanding, not pixel
            # localisation — this is the path that produced a clean redraw of
            # detal_126 where raster tracing gave a mess. It is a redraw, so it
            # is honestly a review_required draft, not a pixel-exact copy.
            if vectorize_method == "spec":
                if not content:
                    return await _fail("Метод «по описанию»: нужен исходный скан/фото.")
                from app.ai.cad_recognize.spec_vectorize import (
                    SpecReaderNotVisionError,
                    SpecReadMalformedError,
                    SpecReadTruncatedError,
                )
                from app.ai.cad_recognize.spec_fragments import (
                    read_spec_best_effort,
                )

                try:
                    import asyncio

                    # Reading once is a bet on a stochastic model; reading a few
                    # times and intersecting turns its inconsistency into an
                    # explicit review item instead of a silent wrong number.
                    passes = int(params.get("read_passes") or 3)
                    await _record(
                        "reader",
                        "started",
                        "Начато многоэтапное чтение чертежа",
                        {"passes": passes, "timeout_seconds": 480},
                    )
                    async with asyncio.timeout(480):
                        spec = await read_spec_best_effort(
                            content,
                            passes=passes,
                            budget_seconds=450,
                        )
                    await _record(
                        "reader",
                        "completed" if spec else "failed",
                        (
                            "Чтение завершено, спецификация получена"
                            if spec
                            else "Чтение завершено без валидной спецификации"
                        ),
                        {
                            "attempts": len(spec.get("reader_attempts") or [])
                            if spec else 0,
                            "has_geometry": bool(
                                spec and (spec.get("main_view") or {}).get("outer")
                            ),
                        },
                    )
                except TimeoutError:
                    spec = await _load_cad_partial_spec(gen_uuid)
                    if not spec:
                        return await _fail(
                            "Метод «по описанию»: чтение остановлено через 480 с.; "
                            "ни один проход не успел сформировать валидную геометрию."
                        )
                    spec.setdefault("optional_unresolved", []).append(
                        "чтение достигло лимита 480 с.; использован последний "
                        "сохранённый consensus"
                    )
                    await _record(
                        "reader.timeout_recovery",
                        "warning",
                        "Лимит чтения достигнут; работа продолжена с последнего "
                        "сохранённого consensus",
                        {
                            "timeout_seconds": 480,
                            "has_geometry": bool((spec.get("main_view") or {}).get("outer")),
                            "partial_spec_sequence": "latest",
                            "_partial_spec": spec,
                            "_progress_pct": 60,
                        },
                    )
                except (
                    SpecReaderNotVisionError,
                    SpecReadTruncatedError,
                    SpecReadMalformedError,
                ) as exc:
                    # A misconfigured slot and a cut-off answer are both
                    # actionable, and neither means "unreadable drawing".
                    return await _fail(f"Метод «по описанию»: {exc}")
                if not spec:
                    return await _fail(
                        "Метод «по описанию»: модель чтения чертежа не вернула "
                        "валидный спек. Проверьте назначение CAD reader (Настройки → "
                        "Модели → Оцифровка) и исходный лист."
                    )
                from app.ai.cad_digitization_type import (
                    validate_spec_for_digitization_type,
                )

                type_blockers = validate_spec_for_digitization_type(
                    spec, digitization_type.normalized
                )
                await _record(
                    "reader.type_gate",
                    "failed" if type_blockers else "completed",
                    (
                        "Прочитанная геометрия не соответствует выбранному типу"
                        if type_blockers
                        else "Тип прочитанной геометрии подтверждён"
                    ),
                    {
                        "requested_type": digitization_type.normalized,
                        "blockers": type_blockers,
                    },
                )
                if type_blockers:
                    return await _fail("; ".join(type_blockers))
                # A value the whole-sheet read missed is not the end of the
                # sheet: asking for that ONE dimension, with its neighbours
                # named, is a far easier question than the one that failed. An
                # answer is accepted only if the sheet's own callouts carry it.
                from app.ai.cad_recognize.spec_followup import (
                    resolve_missing_dimensions,
                )

                try:
                    await _record(
                        "reader.followup", "started",
                        "Начато уточнение недостающих размеров", None,
                    )
                    spec, followup_log = await resolve_missing_dimensions(
                        content, spec
                    )
                except Exception as exc:  # noqa: BLE001 — a follow-up must never cost the read
                    logger.warning(
                        "cad_spec_followup_failed",
                        generation_id=generation_id,
                        error=str(exc)[:200],
                    )
                    followup_log = []
                await _record(
                    "reader.followup",
                    "completed",
                    "Уточнение недостающих размеров завершено",
                    {"questions": len(followup_log)},
                )
                if followup_log:
                    # The spec was re-completed, so the contract has to re-derive
                    # what is still missing — otherwise a value just recovered
                    # would keep blocking the build.
                    spec = _revalidated_spec(spec)
                # What neither the read nor the follow-up could establish is
                # completed from principle — the sheet's own arithmetic where it
                # forces a value, a standard where one fixes it — and marked as
                # assumed. A part with one labelled dimension beats no part:
                # the model is parametric, so the person fixes that dimension
                # in the editor and rebuilds.
                from app.ai.cad_recognize.spec_assumptions import apply_assumptions

                spec, assumptions = apply_assumptions(spec)
                if assumptions:
                    spec = _revalidated_spec(spec)
                # Cross-check before anything is built: the sheet's own
                # arithmetic and the proportions of the traced ink can
                # contradict a read that all passes agreed on.
                from app.ai.cad_recognize.spec_crosscheck import cross_check_spec

                try:
                    check_ink, _cw, _ch = _binarize(content)
                except Exception:  # noqa: BLE001 — a check must not break the run
                    check_ink = None
                crosscheck = cross_check_spec(spec, check_ink)
                blocking_checks = [
                    finding["message"] for finding in crosscheck["findings"]
                    if finding["severity"] == "error"
                ]
                from app.ai.cad_dimension_graph import build_dimension_graph

                dimension_graph = build_dimension_graph(spec)
                blocking_checks.extend(dimension_graph["errors"])
                await _record(
                    "normalize.verify",
                    "completed",
                    "Нормализация, cross-check и граф размеров завершены",
                    {
                        "assumptions": len(assumptions),
                        "crosscheck_findings": len(crosscheck.get("findings") or []),
                        "dimension_errors": len(dimension_graph.get("errors") or []),
                    },
                )

                unresolved = [str(i) for i in spec.get("unresolved", []) if str(i)]
                unresolved.extend(blocking_checks)
                if unresolved:
                    spec = {
                        **spec,
                        "unresolved": list(dict.fromkeys(unresolved)),
                    }
                spec_sheet = str(params.get("sheet_format") or "").upper() or None
                spec_landscape = str(
                    params.get("sheet_orientation") or "landscape"
                ).lower() != "portrait"

                # 3D-first: the part is built, and the sheet is what that part
                # looks like. Not a drawing that happens to have a model beside
                # it — the model IS the drawing's source, so two views cannot
                # disagree and a dimension cannot contradict what it labels.
                solid_result = await _build_spec_solid(
                    spec, generation_id, owner_sub, sheet_format=spec_sheet,
                    landscape=spec_landscape, require_source_evidence=True,
                    source_sha256=normalized_source_sha256,
                    source_uri=normalized_source_path,
                )
                if not solid_result or not solid_result.get("built"):
                    # The part could not be built, but the READING is not lost:
                    # it is the expensive half, it is often nearly right, and a
                    # person can finish it in the editor and rebuild. This is a
                    # review-required draft, not a terminal failure — the same
                    # ``done`` outcome as the post-build unresolved-dimension
                    # path further down, just without a solid to draw from.
                    reason = (solid_result or {}).get("error") or (
                        "по прочитанному не удалось собрать деталь"
                    )
                    blockers = [
                        str(item) for item in (solid_result or {}).get("blockers") or []
                        if str(item)
                    ]
                    if blockers:
                        reason += ": " + "; ".join(blockers[:3])
                        if len(blockers) > 3:
                            reason += f"; и ещё {len(blockers) - 3} — см. журнал"
                    build_note = "3D-тело не построено: " + str(reason)[:700]
                    # The solid did not build, but a 2D draft from the SAME
                    # spec often still can (deterministic for rotation
                    # bodies; generative when one is assigned and the part is
                    # prismatic/complex) — best-effort, never raises past
                    # here, since a missing fallback must not sink the run.
                    fallback_ir = None
                    try:
                        from app.ai.cad_recognize.spec_vectorize import (
                            draft_from_spec_async,
                        )
                        from app.ai.schemas import AITask as _AITask
                        from app.ai.task_routing import get_routing_for as _get_routing_for

                        fallback_ir = await draft_from_spec_async(
                            spec,
                            draft_model=_get_routing_for(_AITask.CAD_SPEC_DRAFT).primary,
                            sheet_format=spec_sheet, landscape=spec_landscape,
                        )
                    except Exception:  # noqa: BLE001 — best-effort fallback only
                        fallback_ir = None
                    revision_created = await _store_failed_reading(
                        factory, gen_uuid, spec, crosscheck, followup_log,
                        unresolved, solid_result, pipeline_manifest,
                        build_note=build_note, fallback_ir=fallback_ir,
                        owner_sub=owner_sub,
                    )
                    await _record(
                        "pipeline",
                        "completed",
                        (
                            "Оцифровка завершена без 3D-тела; сохранён 2D-черновик "
                            "для правки — "
                            if revision_created else
                            "Оцифровка завершена без геометрии; чтение сохранено — "
                        ) + "откройте спецификацию, уточните недостающие размеры "
                        "и пересоберите.",
                        {
                            "solid_built": False,
                            "sheet_built": revision_created,
                            "warnings": len(unresolved),
                        },
                    )
                    return {
                        "ok": True,
                        "generation_id": generation_id,
                        "method": "spec",
                        "review_required": True,
                        "warnings": len(unresolved),
                        "solid_3d": False,
                        "entities": len(fallback_ir.entities) if fallback_ir is not None else 0,
                        "reason": reason,
                    }
                from app.ai.cad_source_projection import evaluate_source_projection

                solid_result["source_projection_verification"] = (
                    evaluate_source_projection(spec, crosscheck, solid_result)
                )
                engineering_graph = solid_result.pop(
                    "_engineering_model_graph", None
                )
                spec_ir = solid_result.pop("_sheet_ir", None)
                if spec_ir is None:
                    return await _fail(
                        "Оцифровка: деталь собрана, но CAD-ядро не смогло построить "
                        "по ней лист. Проверьте доступность cad-kernel."
                    )
                spec_ir.source.generation_id = generation_id
                sheet_without_geometry = False
                _overlay_spec_annotations(spec_ir, spec)
                # Which of the read callouts made it onto the sheet. The old
                # check asked whether the drawn geometry measures the numbers it
                # was drawn FROM, which it cannot help but do; the question that
                # carries information is the opposite one — what did the sheet
                # say that the drawing does not show?
                spec_dim_check = _unplaced_callouts(solid_result, spec)
                solid_result.pop("_dimensions", None)
                validate_ir(spec_ir)
                from app.ai.cad_ir.schema import ValidationIssueIR
                from app.ai.cad_ir.dxf_render import verify_dxf_roundtrip

                dxf_roundtrip = verify_dxf_roundtrip(spec_ir)
                spec_ir.validation.dxf_reopens = bool(dxf_roundtrip.get("ok"))
                solid_result["dxf_roundtrip"] = dxf_roundtrip
                if not dxf_roundtrip.get("ok"):
                    spec_ir.validation.issues.append(
                        ValidationIssueIR(
                            code="DXF_SEMANTIC_ROUNDTRIP_FAILED",
                            severity="error",
                            level=2,
                            message_ru="DXF не прошёл повторное открытие и сверку сущностей/слоёв с CadIR",
                            fix_hint="Исправьте DXF-экспорт до выдачи результата",
                        )
                    )

                if spec_dim_check.get("status") != "ok":
                    spec_ir.validation.issues.append(
                        ValidationIssueIR(
                            code="SPEC_CALLOUTS_UNPLACED",
                            severity="error",
                            level=2,
                            message_ru=(
                                "Не все геометрические размеры исходника размещены: "
                                + ", ".join(spec_dim_check.get("unplaced") or [])
                            ),
                            fix_hint="Добавьте нужный вид/сечение или исправьте связь размера с feature",
                        )
                    )

                if unresolved:
                    spec_ir.validation.issues.append(
                        ValidationIssueIR(
                            code="SPEC_READER_UNRESOLVED",
                            severity="warn",
                            level=3,
                            message_ru=(
                                "Геометрия черновика требует решения пользователя: "
                                + "; ".join(dict.fromkeys(unresolved))
                            ),
                        )
                    )
                # An assumed dimension must be impossible to mistake for a read
                # one. It does not block — the whole point is that the part gets
                # built — but it is stated, per value, with the rule behind it.
                for assumption in assumptions:
                    spec_ir.validation.issues.append(
                        ValidationIssueIR(
                            code=(
                                "SPEC_VALUE_DERIVED"
                                if assumption.origin == "derived"
                                else "SPEC_VALUE_ASSUMED"
                            ),
                            severity="warn",
                            level=3,
                            message_ru=(
                                f"{assumption.path}.{assumption.field} = "
                                f"{assumption.value:g} мм — {assumption.rule}"
                            ),
                            fix_hint="Проверьте по исходному листу и поправьте в редакторе",
                        )
                    )
                # Even a dimensionally consistent redraw is only a proposal:
                # the person comparing it with the source decides whether it is
                # correct, needs editing, or should be rejected.
                spec_ir.digitization_status = "review_required"
                async with factory() as db:
                    gen = await db.get(ImageGeneration, gen_uuid)
                    if not gen or gen.status == ImageGenStatus.cancelled:
                        return {"cancelled": True}
                    normalized_path = (
                        f"image-gen/{gen.owner_sub or 'shared'}/{gen.id}_normalized.png"
                    )
                    upload_file(content, normalized_path, "image/png")
                    graph_row = None
                    graph_ref = None
                    if engineering_graph is not None:
                        from app.services.engineering_model_graph import (
                            persist_pipeline_graph,
                        )

                        graph_row = await persist_pipeline_graph(db, engineering_graph)
                        graph_ref = {
                            "revision_id": str(graph_row.id),
                            "graph_id": graph_row.graph_id,
                            "revision": graph_row.revision,
                            "canonical_sha256": graph_row.canonical_sha256,
                        }
                    gen.params = {
                        **(gen.params or {}),
                        "vectorize_method": "spec",
                        "description_mode": False,
                        "spec": spec,
                        "spec_dimension_check": spec_dim_check,
                        "spec_crosscheck": crosscheck,
                        "dimension_graph": dimension_graph,
                        # Which values a second look recovered, which it failed
                        # to, and which answers were refused for having no
                        # callout behind them.
                        "spec_followup": followup_log,
                        "cad_reading": {
                            "spec": spec,
                            "attempts": spec.get("reader_attempts") or [],
                            "reader_models": (
                                pipeline_manifest.get("components", {})
                                .get("spec_reader", {})
                                .get("models", [])
                            ),
                            "followup": followup_log,
                            "crosscheck": crosscheck,
                            "dimension_graph": dimension_graph,
                            "unresolved": list(dict.fromkeys(unresolved)),
                            "assumptions": [item.as_dict() for item in assumptions],
                        },
                        # Every value that was completed rather than read, with
                        # the rule behind it — the review panel's own list.
                        "spec_assumptions": [item.as_dict() for item in assumptions],
                        "spec_review_warnings": list(dict.fromkeys(unresolved)),
                        "sheet_without_geometry": sheet_without_geometry,
                        "cad_pipeline_manifest": pipeline_manifest,
                        "normalized_source_path": normalized_path,
                        "solid_input": (solid_result or {}).get("kernel_input"),
                        **({"solid_3d": solid_result} if solid_result else {}),
                        **(
                            {"engineering_model_graph": graph_ref}
                            if graph_ref is not None else {}
                        ),
                    }
                    cad_revision = await cad_ir_store.save_revision(
                        db, gen, spec_ir, origin="auto", created_by=owner_sub,
                        keep_raster=None, thin_px=2, thick_px=4,
                    )
                    if graph_row is not None:
                        cad_revision.engineering_graph_revision_id = graph_row.id
                    gen.status = ImageGenStatus.done
                    job = await studio_queue.job_for_generation(db, gen_uuid)
                    await studio_queue.mark_job_done(db, job)
                    await db.commit()
                await _record(
                    "pipeline",
                    "completed",
                    "Оцифровка завершена; чтение и результат сохранены",
                    {
                        "entities": len(spec_ir.entities),
                        "solid_built": bool(solid_result and solid_result.get("built")),
                        "build_status": (solid_result or {}).get("build_status", "blocked"),
                    },
                )
                return {
                    "ok": True, "generation_id": generation_id,
                    "entities": len(spec_ir.entities), "method": "spec",
                    "review_required": True,
                    "warnings": len(unresolved),
                    "solid_3d": bool(solid_result and solid_result.get("built")),
                }

            from app.ai.cad_recognize.spec_vectorize import (
                draft_from_spec_async,
                read_description_spec,
            )
            from app.ai.schemas import AITask
            from app.ai.task_routing import get_routing_for

            spec = await read_description_spec(description)
            if not spec:
                return await _fail(
                    "Метод «по описанию»: модель чтения не вернула валидную "
                    "EngineeringDrawingSpec. Проверьте назначение CAD reader и описание."
                )
            unresolved = [str(item) for item in spec.get("unresolved", []) if str(item)]
            if unresolved:
                return await _fail(
                    "Метод «по описанию»: построение остановлено — не определены "
                    "обязательные данные: " + ", ".join(unresolved[:8])
                )
            # Model 2: a generative drafter when one is assigned in Settings →
            # Models → Оцифровка → «Чертёжник» (e.g. a LoRA); else deterministic.
            draft_model = get_routing_for(AITask.CAD_SPEC_DRAFT).primary
            # Sheet + orientation → auto scale (ГОСТ 2.302). No sheet → free-fit.
            spec_sheet = str(params.get("sheet_format") or "").upper() or None
            spec_landscape = str(
                params.get("sheet_orientation") or "landscape"
            ).lower() != "portrait"
            spec_ir = await draft_from_spec_async(
                spec,
                draft_model=draft_model,
                sheet_format=spec_sheet,
                landscape=spec_landscape,
            )
            if spec_ir is None:
                return await _fail(
                    "Метод «по описанию»: назначенный чертёжник не смог построить "
                    "поддерживаемую параметрическую геометрию для этого профиля. "
                    "Попробуйте другую модель в Настройки → Модели → Оцифровка "
                    "или используйте трассировку с обязательной проверкой."
                )
            spec_ir.source.generation_id = generation_id
            _overlay_spec_annotations(spec_ir, spec)
            validate_ir(spec_ir)
            async with factory() as db:
                gen = await db.get(ImageGeneration, gen_uuid)
                if not gen or gen.status == ImageGenStatus.cancelled:
                    return {"cancelled": True}
                normalized_path = None
                if content:
                    normalized_path = f"image-gen/{gen.owner_sub or 'shared'}/{gen.id}_normalized.png"
                    upload_file(content, normalized_path, "image/png")
                gen.params = {
                    **(gen.params or {}),
                    "vectorize_method": "text_spec",
                    "description_mode": description_mode,
                    "spec": spec,
                    "cad_pipeline_manifest": pipeline_manifest,
                }
                if normalized_path:
                    gen.params["normalized_source_path"] = normalized_path
                await cad_ir_store.save_revision(
                    db, gen, spec_ir, origin="auto", created_by=owner_sub,
                    keep_raster=None, thin_px=2, thick_px=4,
                )
                gen.status = ImageGenStatus.done
                job = await studio_queue.job_for_generation(db, gen_uuid)
                await studio_queue.mark_job_done(db, job)
                await db.commit()
            return {
                "ok": True, "generation_id": generation_id,
                "entities": len(spec_ir.entities), "method": "spec",
            }

        # Stage 1: classical preprocess — same module the cleanup path trusts.
        try:
            from app.ai.drawing_cleanup import enhance_source_for_diffusion

            content = enhance_source_for_diffusion(content)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("cad_trace_enhance_failed", generation_id=generation_id, error=str(exc))

        # Stage 2: binarize.
        ink, w, h = _binarize(content)

        # Stage 3: OCR text (annotations + exclusion zones). When VLM
        # enrichment is on, keep low-confidence plausible reads so the VLM can
        # rescue them (Stage 3.5); otherwise filter them out to keep the
        # drawing clean.
        # VLM enrichment is ON by default (2026-07-17): tesseract alone
        # misreads dense CAD labels, and a local vision model (qwen3-vl)
        # re-reads the uncertain crops far better. Pass vlm_dimensions=false
        # to opt out (e.g. hosts without a vision model).
        vlm_enrich = params.get("vlm_dimensions", True)
        # Tesseract still runs — but only to supply exclusion boxes that keep
        # text ink out of the geometry tracer. It is a weak *reader*: it cannot
        # even detect isolated single glyphs or sub-10px title-block text, so a
        # tesseract-first text layer is capped by its detection blind spots.
        tess_texts, text_boxes = _ocr_text_entities(content, lenient=bool(vlm_enrich))
        text_entities = tess_texts

        # Stage 3.5: primary text layer from a local vision model. qwen3-vl
        # reads whole-sheet text (single letters/digits, small annotations)
        # that tesseract never detects, and grounds each read with a box.
        # Confidential: local model only. Falls back to tesseract + per-crop
        # enrichment when no VLM is reachable.
        vlm_texts: list = []
        if vlm_enrich:
            try:
                from app.ai.vlm_dimensions import read_sheet_text_entities

                vlm_texts = await read_sheet_text_entities(content, confidential=True)
            except Exception as exc:  # noqa: BLE001 — text read must never sink digitize
                logger.warning("cad_trace_vlm_sheet_text_failed", error=str(exc))
                vlm_texts = []

        if vlm_texts:
            # VLM read is the primary text layer.
            text_entities = vlm_texts
            logger.info("cad_trace_vlm_sheet_text", texts=len(vlm_texts))
        elif vlm_enrich:
            # No VLM sheet read (model down/empty) → tesseract + per-crop
            # enrichment, then the strict post-VLM filter (previous behavior).
            try:
                await _enrich_text_with_vlm(text_entities, content)
            except Exception as exc:  # noqa: BLE001 — enrichment must never sink digitize
                logger.warning("cad_trace_vlm_enrich_failed", error=str(exc))
            before = len(text_entities)
            text_entities = [
                e for e in text_entities
                if e.confidence * 100 >= (
                    _TEXT_MIN_CONF_SINGLE if len((e.text or "").strip()) <= 1
                    else _TEXT_MIN_CONF_SHORT if len((e.text or "").strip()) == 2
                    else _TEXT_MIN_CONF_LONG
                )
            ]
            if before != len(text_entities):
                logger.info(
                    "cad_trace_post_vlm_text_filter",
                    kept=len(text_entities), dropped=before - len(text_entities),
                )

        from app.ai.cad_profile import choose_profile

        profile_decision = choose_profile(
            requested_profile,
            [entity.text for entity in text_entities],
            str(params.get("source_filename") or ""),
        )

        # Stage 4: scale (manual override wins; else frame detection).
        manual_scale = params.get("scale_mm_per_px")
        confirmed_format = str(params.get("sheet_format") or "").upper() or None
        sheet_format = None
        frame_quad = None
        scale_source = None
        if manual_scale:
            scale = float(manual_scale)
            scale_source = "manual"
        else:
            frame_quad = _detect_sheet_frame_quad(ink, w, h)
            scale, sheet_format = (
                _scale_from_quad(frame_quad, w, h, confirmed_format)
                if frame_quad is not None
                else (None, None)
            )
            if scale is not None:
                scale_source = "sheet_format"

        # Stage 4.5 (Ф4.4): title block (основная надпись) presence, purely
        # geometric — no OCR/VLM read of its FIELDS yet, just "is there one
        # and where" so the UI/normcontrol can point at it. The one field we
        # DO read here: a stated scale ("М 1:2") from OCR text that already
        # landed inside the region — the real producer for
        # ESKD_SCALE_NONSTANDARD's title_block["scale"] check.
        title_block = _detect_title_block(ink, w, h) or {}
        if title_block:
            stamp_scale = _extract_stamp_scale(text_entities, title_block["region"])
            if stamp_scale:
                title_block["scale"] = stamp_scale

        # Stage 5: recognize geometry — neural (if available) arbitrated
        # against CV by independent coverage scoring, never by the model's
        # own say-so (see cad_recognize/verify.arbitrate_recognition).
        # Do NOT pre-exclude text regions from tracing: on a real drawing the
        # dimension text sits ON the part (leader/dimension lines, hatching),
        # so blanking text boxes deletes the geometry too — measured on
        # detal_126 it dropped the main shaft body, ~78% of segments (2619 ->
        # 584). Instead trace everything and remove only segments that lie
        # ENTIRELY inside a text glyph's tight box afterwards.
        arbitration = arbitrate_recognition(ink, None, TechnicalVectorizerRecognizer(), CvRecognizer())
        # B1: an empty recognition is a hard failure only when the sheet
        # itself is pathological (no ink at all / near-solid black). Anything
        # in between degrades to a reviewable draft: the ink ships as raster
        # passthrough with whatever frame/text WAS recognized, flagged
        # RECOGNITION_EMPTY — the user reviews and traces in the editor
        # instead of hitting a dead "лист слишком плотный или пустой" error.
        degraded_recognition = not arbitration.entities
        ink_fraction = float((ink > 0).mean())
        if degraded_recognition and not 0.0 < ink_fraction <= _DEGRADED_MAX_INK_FRACTION:
            return await _fail(
                "Не удалось распознать линейную графику: лист "
                + ("пустой. " if ink_fraction <= 0.0 else "почти полностью залит — не похож на линейный чертёж. ")
                + "Попробуйте сначала пропустить фото через режим «Очистка»."
            )
        keep_raster = arbitration.keep_raster
        thin_px, thick_px = arbitration.thin_px, arbitration.thick_px
        if degraded_recognition:
            keep_raster = ink > 0

        # The frame quad (Stage 4) is emitted as real Segment entities here,
        # not just used for scale — see _frame_segments_from_quad for why
        # skeleton-traced fragments alone systematically under-recognize a
        # sheet border. Coverage is rescored below to reflect it.
        frame_segments = _frame_segments_from_quad(frame_quad) if frame_quad is not None else []

        # Stage 5.4 (B2): reconstruct dimensions from OCR value labels paired
        # with the thin lines they annotate — a размер is a dimension line +
        # value, not floating text over a stroke. Deterministic; anything
        # ambiguous stays as separate text + line. Frame segments are contour,
        # not thin, so they can never be consumed here.
        recognized = _drop_in_glyph_segments(arbitration.entities, text_entities)
        geometry, text_entities, dim_count = reconstruct_dimensions(
            recognized, text_entities, scale, w, h,
        )

        frame_px = None
        if frame_quad is not None:
            import cv2

            fx, fy, fw, fh = cv2.boundingRect(frame_quad)
            frame_px = [float(fx), float(fy), float(fw), float(fh)]

        ir = CadIR(
            source=SourceInfo(
                generation_id=generation_id, image_width=w, image_height=h, kind="scan"
            ),
            scale=scale,
            scale_source=scale_source,
            sheet=SheetInfo(
                format=sheet_format,
                frame=frame_quad is not None,
                title_block=title_block,
                frame_px=frame_px,
            ),
            entities=[*geometry, *frame_segments, *text_entities],
            recognizer_used=arbitration.recognizer_used,
        )

        # Stage 5.5 (Ф4.3, opt-in via params): escalate ambiguous thin lines
        # (axis/hidden/dim all render as the same thin stroke in raster —
        # the CV width-only heuristic can't tell them apart) to a VLM crop
        # classification. Same non-blocking, off-by-default contract as
        # Stage 3.5.
        if params.get("vlm_lines"):
            await _enrich_lines_with_vlm(ir, content)

        # Stage 6: verification score. Rescored when frame segments were
        # added (Stage 5) or dimensions reconstructed (Stage 5.4) —
        # arbitration.score predates both and would misreport recall.
        # Dimension leader lines still rasterize, so coverage is preserved.
        score = (
            score_coverage(
                [*geometry, *frame_segments], ink,
                keep_raster, thin_px, thick_px,
            )
            if (frame_segments or dim_count)
            else arbitration.score
        )
        apply_to_ir(ir, score)

        # Stage 6.7 (Ф4.2/4.3): cross-check any VLM reading/line-class
        # hypotheses attached in Stage 3.5/5.5 — promotes a decisive winner,
        # queues genuine ambiguity for human review. No-op when nothing has
        # alternatives.
        from app.ai.cad_hypothesis import resolve_hypotheses, resolve_line_hypotheses

        resolve_hypotheses(ir)
        resolve_line_hypotheses(ir)

        _assess_export_fidelity(ir, ink, keep_raster, thin_px, thick_px)
        validate_ir(ir)

        # Recognition-provenance signals are appended AFTER validate_ir:
        # validate_ir rebuilds the issue list from IR-derivable checks (plus
        # sticky DIFFUSION_*) and cannot re-derive these pipeline facts —
        # appended before it, they were silently wiped (pre-existing bug for
        # NEURAL_UNAVAILABLE/RECOGNIZER_DISCREPANCY). Their lifecycle is
        # intentionally revision-0-only: the next revalidation after a human
        # edit drops them, while quality gating stays with COVERAGE_LOW.
        from app.ai.cad_ir.schema import ValidationIssueIR

        if degraded_recognition:
            ir.validation.issues.append(ValidationIssueIR(
                code="RECOGNITION_EMPTY", severity="error",
                message_ru=(
                    "Векторная геометрия не распознана — лист сохранён растровой подложкой "
                    "с рамкой и текстом. Проверьте исходник, попробуйте режим «Очистка» "
                    "или обведите геометрию вручную в редакторе."
                ),
            ))
        if not arbitration.neural_available:
            ir.validation.issues.append(ValidationIssueIR(
                code="NEURAL_UNAVAILABLE", severity="info",
                message_ru="Нейросетевой распознаватель недоступен — использован классический CV-путь.",
            ))
        if arbitration.discrepancy:
            n = arbitration.notes
            ir.validation.issues.append(ValidationIssueIR(
                code="RECOGNIZER_DISCREPANCY", severity="warn",
                message_ru=(
                    f"Нейросеть и классический CV дали расходящиеся результаты "
                    f"({n.get('neural_entities')} vs {n.get('cv_entities')} элементов) "
                    f"— использован результат {arbitration.recognizer_used}, сверьте с оригиналом."
                ),
            ))

        if degraded_recognition or not arbitration.neural_available or arbitration.discrepancy:
            ir.digitization_status = "review_required"

        # Stage 6.5: pixel provenance for diffusion-derived sources — diffusion
        # output is not a truth source. Findings are sticky (survive later
        # revalidation) until the flagged entities are resolved by a human.
        if parent_operation in ("cleanup", "edit", "inpaint", "eskd", "generate"):
            from app.ai.cad_ir.schema import ReviewItem, ValidationIssueIR
            from app.ai.pixel_provenance import (
                diffusion_change_masks,
                entities_in_mask,
                mask_regions,
                uncovered_added_regions,
            )

            masks = None
            if parent_source_path:
                try:
                    masks = diffusion_change_masks(ink, download_file(parent_source_path))
                except Exception:  # noqa: BLE001
                    masks = None
            if masks is not None:
                added, removed = masks
                flagged = entities_in_mask(
                    ir.entities, added, arbitration.thin_px, arbitration.thick_px
                )
                logger.info("diffusion_provenance", flagged=len(flagged), removed_px=int(removed.sum()))
                if flagged:
                    ir.validation.issues.append(ValidationIssueIR(
                        code="DIFFUSION_ADDED_INK",
                        severity="warn",
                        entity_ids=flagged,
                        message_ru=(
                            f"{len(flagged)} элемент(ов) распознаны из областей, ДОРИСОВАННЫХ "
                            "диффузионной очисткой, — их не было на исходном фото. Подтвердите или удалите."
                        ),
                    ))
                    queued = {r.entity_id for r in ir.review}
                    for eid in flagged:
                        if eid not in queued:
                            ir.review.append(ReviewItem(entity_id=eid, reason="diffusion_modified"))
                # Added ink that never became an entity (raster-passthrough
                # zones like OCR exclusions) must not ship silently either.
                flagged_entities = [e for e in ir.entities if e.id in set(flagged)]
                orphan_boxes = uncovered_added_regions(
                    added, flagged_entities, arbitration.thin_px, arbitration.thick_px
                )
                if orphan_boxes:
                    ir.validation.issues.append(ValidationIssueIR(
                        code="DIFFUSION_ADDED_INK",
                        severity="warn",
                        message_ru=(
                            f"Диффузионная очистка ДОРИСОВАЛА графику в {len(orphan_boxes)} "
                            f"растровых зон(ах), не ставших элементами (крупнейшая: {orphan_boxes[0]}). "
                            "Сверьте эти области с оригиналом."
                        ),
                    ))
                boxes = mask_regions(removed)
                if boxes:
                    ir.validation.issues.append(ValidationIssueIR(
                        code="DIFFUSION_REMOVED_INK",
                        severity="warn",
                        message_ru=(
                            f"Диффузионная очистка СТЁРЛА {len(boxes)} участок(ов) исходной графики "
                            f"(крупнейший: {boxes[0]}). Сверьте с оригиналом."
                        ),
                    ))
            else:
                ir.validation.issues.append(ValidationIssueIR(
                    code="DIFFUSION_SOURCE_UNVERIFIED",
                    severity="warn",
                    message_ru=(
                        "Источник — результат генеративной модели, и сверка с оригиналом недоступна: "
                        "происхождение графики не подтверждено. Проверяйте размеры по бумажному оригиналу."
                    ),
                ))

        # Stage 7: persist revision 0 + renders.
        async with factory() as db:
            gen = await db.get(ImageGeneration, gen_uuid)
            if not gen or gen.status == ImageGenStatus.cancelled:
                return {"cancelled": True}
            normalized_path = f"image-gen/{gen.owner_sub or 'shared'}/{gen.id}_normalized.png"
            upload_file(content, normalized_path, "image/png")
            gen.params = {
                **(gen.params or {}),
                "normalized_source_path": normalized_path,
                "digitization_profile": profile_decision.profile,
                "digitization_type": digitization_type.normalized,
                "digitization_profile_confidence": profile_decision.confidence,
                "digitization_profile_evidence": list(profile_decision.evidence),
                "cad_pipeline_manifest": build_cad_pipeline_manifest(
                    profile=profile_decision.profile,
                    method="trace",
                    source_sha256=source_sha256,
                    digitization_type=digitization_type.normalized,
                ),
            }
            await cad_ir_store.save_revision(
                db, gen, ir,
                origin="auto",
                created_by=owner_sub,
                keep_raster=keep_raster,
                thin_px=thin_px,
                thick_px=thick_px,
            )
            gen.status = ImageGenStatus.done
            job = await studio_queue.job_for_generation(db, gen_uuid)
            await studio_queue.mark_job_done(db, job)
            await db.commit()

            if owner_sub:
                try:
                    from app.services import push

                    await push.push_to_user(
                        db=db,
                        user_sub=owner_sub,
                        title="Оцифровка готова",
                        body="Чертёж распознан — открыт в CAD-редакторе, DXF и проверка доступны.",
                        action_url=f"/cad/{generation_id}",
                        notification_type="image_ready",
                    )
                except Exception:  # noqa: BLE001
                    pass

        return {
            "ok": True,
            "generation_id": generation_id,
            "entities": len(ir.entities),
            "coverage": [arbitration.score.recall, arbitration.score.precision],
            "recognizer_used": arbitration.recognizer_used,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("cad_trace_failed", generation_id=generation_id, error=str(exc))
        return await _fail(f"{type(exc).__name__}: {exc}")
    finally:
        reset_cad_process_recorder(recorder_token)
