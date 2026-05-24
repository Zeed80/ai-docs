"""Celery task: agent-driven tech process generation from drawing.

Pipeline (9 steps):
  1. drawing_features   — load DrawingFeature records
  2. surface_analysis   — extract SurfaceMachiningSpec candidates
  3. blank_selection    — recommend blank type / BlankSpec
  4. operation_drafting — group surfaces → ManufacturingOperation rows
  5. equipment_matching — assign machines to operations
  6. cutting_params     — update cutting_parameters on each operation
  7. time_norms         — calculate To/Tv/Tsht/Tsht-k
  8. normcontrol        — run NormControl checks (if auto_normcontrol=True)
  9. graph_update       — build knowledge-graph edges drawing→plan→operations
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()

TP_PIPELINE_STEPS = [
    ("drawing_features",   "Загрузка признаков чертежа"),
    ("surface_analysis",   "Анализ поверхностей"),
    ("blank_selection",    "Подбор заготовки"),
    ("operation_drafting", "Формирование операций"),
    ("equipment_matching", "Подбор оборудования"),
    ("cutting_params",     "Расчёт режимов резания"),
    ("time_norms",         "Нормирование"),
    ("normcontrol",        "Нормоконтроль"),
    ("graph_update",       "Обновление графа знаний"),
]


def _get_sync_session() -> Session:
    engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return Session(engine)


def _default_steps() -> list[dict]:
    return [{"key": k, "label": l, "status": "pending"} for k, l in TP_PIPELINE_STEPS]


def _set_step(steps: list[dict], key: str, status: str, error: str | None = None) -> list[dict]:
    result = []
    for s in steps:
        if s["key"] == key:
            s = {**s, "status": status}
            if error:
                s["error"] = error
        result.append(s)
    return result


@celery_app.task(
    bind=True,
    name="tp_generation.generate_from_drawing",
    max_retries=2,
    soft_time_limit=300,
    time_limit=360,
)
def generate_tp_from_drawing(
    self,
    plan_id: str,
    drawing_id: str,
    batch_size: int = 1,
    auto_normcontrol: bool = True,
    created_by: str = "sveta",
) -> dict[str, Any]:
    """
    Full tech-process generation pipeline for plan_id from drawing_id.
    Runs synchronously inside Celery worker.
    """
    try:
        return asyncio.get_event_loop().run_until_complete(
            _generate_tp_async(
                plan_id=plan_id,
                drawing_id=drawing_id,
                batch_size=batch_size,
                auto_normcontrol=auto_normcontrol,
                created_by=created_by,
            )
        )
    except Exception as exc:
        logger.error("tp_generation_failed", plan_id=plan_id, error=str(exc))
        raise self.retry(exc=exc, countdown=15)


async def _generate_tp_async(
    plan_id: str,
    drawing_id: str,
    batch_size: int,
    auto_normcontrol: bool,
    created_by: str,
) -> dict[str, Any]:
    from app.db.models import (
        Drawing,
        DrawingStatus,
        ManufacturingOperation,
        ManufacturingProcessPlan,
        ManufacturingResource,
        SurfaceMachiningSpec,
    )
    from app.ai.tp_generator import (
        calculate_cutting_parameters,
        calculate_time_norms,
        draft_operations_from_surfaces,
        extract_tp_features_from_drawing,
        link_surfaces_to_operations,
        recommend_blank,
        save_surface_specs,
        select_equipment,
    )
    from app.ai.normcontrol_agent import run_normcontrol

    plan_uuid = uuid.UUID(plan_id)
    drawing_uuid = uuid.UUID(drawing_id)

    db = _get_sync_session()
    steps = _default_steps()

    try:
        plan = db.query(ManufacturingProcessPlan).filter(
            ManufacturingProcessPlan.id == plan_uuid
        ).first()
        if not plan:
            raise ValueError(f"ProcessPlan {plan_id} not found")

        drawing = db.query(Drawing).filter(Drawing.id == drawing_uuid).first()
        if not drawing:
            raise ValueError(f"Drawing {drawing_id} not found")

        # Guard: drawing must be fully analyzed before TP generation
        if drawing.status not in (DrawingStatus.analyzed, DrawingStatus.needs_review):
            from app.tasks.drawing_analysis import analyze_drawing
            if drawing.status == DrawingStatus.uploaded:
                analyze_drawing.delay(str(drawing_id), None, False, 6, None)
            raise ValueError(
                f"Чертёж ещё не обработан (статус: {drawing.status.value}). "
                "Анализ запущен автоматически, повторите через несколько минут."
            )

        material = plan.material or (drawing.title_block or {}).get("material", "Сталь 45") or "Сталь 45"

        def _save_steps():
            plan.metadata_ = {**(plan.metadata_ or {}), "tp_pipeline_steps": steps}
            db.flush()
            db.commit()

        # ── Step 1: drawing_features ──────────────────────────────────────────
        steps = _set_step(steps, "drawing_features", "running")
        _save_steps()

        features = drawing.features
        if not features:
            logger.warning("tp_no_drawing_features", drawing_id=drawing_id)

        steps = _set_step(steps, "drawing_features", "done")
        _save_steps()

        # ── Step 2: surface_analysis ──────────────────────────────────────────
        steps = _set_step(steps, "surface_analysis", "running")
        _save_steps()

        surface_dicts = extract_tp_features_from_drawing(drawing, plan_uuid, db)
        surface_rows = save_surface_specs(surface_dicts, db)

        steps = _set_step(steps, "surface_analysis", "done")
        _save_steps()

        # ── Step 3: blank_selection ───────────────────────────────────────────
        steps = _set_step(steps, "blank_selection", "running")
        _save_steps()

        from app.db.models import BlankSpec

        dims: dict[str, float] = {}
        if drawing.title_block:
            mass_kg = drawing.title_block.get("mass_kg")
        else:
            mass_kg = None

        # Use 3D bounding box from STEP/IGES if available (precise КИМ calculation)
        bounding_box_mm = drawing.bounding_box if drawing.bounding_box else None
        blank_data = recommend_blank(
            material, dims, mass_kg,
            annual_volume=batch_size,
            bounding_box_mm=bounding_box_mm,
        )
        existing_blank = db.query(BlankSpec).filter(
            BlankSpec.process_plan_id == plan_uuid
        ).first()
        if existing_blank:
            for k, v in blank_data.items():
                setattr(existing_blank, k, v)
            blank_spec = existing_blank
        else:
            blank_spec = BlankSpec(process_plan_id=plan_uuid, **blank_data)
            db.add(blank_spec)
        db.flush()
        plan.blank_type = blank_data["blank_type"]

        steps = _set_step(steps, "blank_selection", "done")
        _save_steps()

        # ── Step 4: operation_drafting ────────────────────────────────────────
        steps = _set_step(steps, "operation_drafting", "running")
        _save_steps()

        # Remove any existing operations (re-draft)
        for existing_op in list(plan.operations):
            db.delete(existing_op)
        db.flush()

        operations = draft_operations_from_surfaces(
            surface_dicts, material, batch_size, plan_uuid, db
        )

        # Link surfaces to operations
        link_surfaces_to_operations(surface_rows, operations, db)

        steps = _set_step(steps, "operation_drafting", "done")
        _save_steps()

        # ── Step 5: equipment_matching ────────────────────────────────────────
        steps = _set_step(steps, "equipment_matching", "running")
        _save_steps()

        for op in operations:
            if op.operation_type in ("blank_preparation", "quality_control"):
                continue
            candidates = select_equipment(op.operation_type, None, None, db, limit=1)
            if candidates:
                op.machine_resource_id = candidates[0].id
        db.flush()

        steps = _set_step(steps, "equipment_matching", "done")
        _save_steps()

        # ── Step 6: cutting_params ────────────────────────────────────────────
        steps = _set_step(steps, "cutting_params", "running")
        _save_steps()

        for op in operations:
            if op.operation_type not in {"turning", "milling", "drilling", "grinding",
                                          "boring", "reaming", "honing", "broaching"}:
                continue
            if not op.cutting_parameters:
                # Find first surface for this operation
                nominal_mm = None
                roughness_ra = None
                linked = db.query(SurfaceMachiningSpec).filter(
                    SurfaceMachiningSpec.operation_id == op.id
                ).first()
                if linked:
                    nominal_mm = linked.nominal_mm
                    roughness_ra = linked.roughness_ra

                cp = calculate_cutting_parameters(
                    op.operation_type, material, nominal_mm, roughness_ra
                )
                op.cutting_parameters = cp
                op.to_minutes = cp["to_min"]
        db.flush()

        steps = _set_step(steps, "cutting_params", "done")
        _save_steps()

        # ── Step 7: time_norms ────────────────────────────────────────────────
        steps = _set_step(steps, "time_norms", "running")
        _save_steps()

        for op in operations:
            if op.operation_type in ("blank_preparation", "quality_control"):
                continue
            if op.to_minutes is None:
                continue
            norms = calculate_time_norms(op.operation_type, op.to_minutes, batch_size)
            for field, val in norms.items():
                if hasattr(op, field):
                    setattr(op, field, val)
        db.flush()

        # Update plan total norms
        total = sum(
            (op.tsht_k_minutes or 0.0)
            for op in operations
            if op.tsht_k_minutes is not None
        )
        plan.total_norm_minutes = round(total, 2)

        steps = _set_step(steps, "time_norms", "done")
        _save_steps()

        # ── Step 8: normcontrol ───────────────────────────────────────────────
        normcontrol_result: dict[str, Any] = {}
        if auto_normcontrol:
            steps = _set_step(steps, "normcontrol", "running")
            _save_steps()

            normcontrol_result = run_normcontrol(plan_uuid, db)

            steps = _set_step(steps, "normcontrol", "done")
            _save_steps()
        else:
            steps = _set_step(steps, "normcontrol", "skipped")
            _save_steps()

        # ── Step 9: graph_update ──────────────────────────────────────────────
        steps = _set_step(steps, "graph_update", "running")
        _save_steps()

        _try_update_graph(plan_uuid, drawing_uuid, operations, db)

        steps = _set_step(steps, "graph_update", "done")
        _save_steps()

        # Final commit
        plan.metadata_ = {**(plan.metadata_ or {}), "tp_pipeline_steps": steps, "tp_completed_at": datetime.now(timezone.utc).isoformat()}
        db.commit()

        result = {
            "plan_id": plan_id,
            "drawing_id": drawing_id,
            "surfaces_count": len(surface_rows),
            "operations_count": len(operations),
            "total_norm_minutes": plan.total_norm_minutes,
            "blank_spec": blank_data,
            "normcontrol": normcontrol_result,
            "status": "completed",
        }

        logger.info("tp_generation_completed", **{k: v for k, v in result.items() if k != "blank_spec"})
        return result

    except Exception as exc:
        try:
            steps = _set_step(steps, _current_running_step(steps), "failed", error=str(exc))
            plan.metadata_ = {**(plan.metadata_ or {}), "tp_pipeline_steps": steps, "tp_error": str(exc)}
            db.commit()
        except Exception:
            pass
        raise
    finally:
        db.close()


def _current_running_step(steps: list[dict]) -> str:
    for s in steps:
        if s.get("status") == "running":
            return s["key"]
    return "unknown"


def _try_update_graph(
    plan_id: uuid.UUID,
    drawing_id: uuid.UUID,
    operations: list,
    db: Session,
) -> None:
    """Attempt to create knowledge graph edges. Non-fatal if graph unavailable."""
    try:
        from app.db.models import KnowledgeEdge, KnowledgeNode

        def _get_or_create_node(entity_type: str, entity_id: uuid.UUID, label: str) -> KnowledgeNode:
            node = db.query(KnowledgeNode).filter(
                KnowledgeNode.entity_type == entity_type,
                KnowledgeNode.entity_id == entity_id,
            ).first()
            if not node:
                node = KnowledgeNode(entity_type=entity_type, entity_id=entity_id, label=label)
                db.add(node)
                db.flush()
            return node

        drawing_node = _get_or_create_node("drawing", drawing_id, f"Drawing {drawing_id}")
        plan_node = _get_or_create_node("process_plan", plan_id, f"ТП {plan_id}")

        edge = KnowledgeEdge(
            source_node_id=drawing_node.id,
            target_node_id=plan_node.id,
            edge_type="derived_tp",
            weight=1.0,
        )
        db.add(edge)

        for op in operations:
            op_node = _get_or_create_node("operation", op.id, op.name)
            op_edge = KnowledgeEdge(
                source_node_id=plan_node.id,
                target_node_id=op_node.id,
                edge_type="contains_operation",
                weight=1.0,
            )
            db.add(op_edge)

        db.flush()

    except Exception as exc:
        logger.warning("tp_graph_update_failed", error=str(exc))
