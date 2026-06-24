"""Spec-driven workspace tables: build from a declarative spec, edit by patch.

The agent (or a deterministic command parser) supplies a :class:`TableSpec`;
data always flows SQL → workspace block directly, never through an LLM, so
tables are complete (true ``total``) and instant. The spec is stored inside
the block, which makes edits idempotent patch operations.

Skills:
- ``workspace.spec_table``        — POST /api/workspace/agent/spec-table
- ``workspace.spec_table_patch``  — POST /api/workspace/agent/spec-table/patch
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

import json
import re
import uuid

from app.core.chat_bus import chat_bus
from app.db.models import Approval, ApprovalActionType, DraftAction
from app.db.session import get_db
from app.domain.table_spec import (
    SOURCES,
    PatchOp,
    TableSpec,
    apply_patch,
    execute_spec,
    parse_patch_command,
    validate_spec,
    writeback_for,
)
from app.domain.workspace import get_workspace_block, upsert_workspace_block

logger = structlog.get_logger()

router = APIRouter()

DEFAULT_CANVAS = "agent:spec-table"


class SpecTableRequest(BaseModel):
    # Lenient by design — weaker/thinking models mangle structured args, so we
    # accept the spec however it arrives and normalise it (see _effective_spec):
    #  • {"spec": {...}}              — canonical
    #  • {"spec": "{...json...}"}     — spec as a JSON string
    #  • {"source": ..., "columns": ...}  — fields flattened to the top level
    # Anything that still can't be parsed yields a structured, actionable error
    # (action=spec_table_catalog), never a bare 422 the agent can't recover from.
    model_config = ConfigDict(extra="allow")

    canvas_id: str = DEFAULT_CANVAS
    spec: Any = None
    title: str | None = None
    # Flattened-spec fallbacks.
    source: str | None = None
    columns: Any = None
    filters: Any = None
    sort: Any = None
    group_by: Any = None
    limit: Any = None


def _maybe_json(value: Any) -> Any:
    """json.loads a string, else return the value unchanged."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _coerce_spec_fields(spec: dict[str, Any]) -> dict[str, Any]:
    """Normalise sub-fields a weak/thinking model tends to mangle.

    Handles: columns/filters/sort/group_by arriving as JSON strings; columns as
    a comma-separated field list or bare-string entries; numeric limit as a
    string. Anything unrecoverable is left for validate_spec to report cleanly.
    """
    out = dict(spec)

    cols = _maybe_json(out.get("columns"))
    if isinstance(cols, str):
        cols = [c.strip() for c in re.split(r"[;,\n]", cols) if c.strip()]
    if isinstance(cols, list):
        out["columns"] = [
            {"field": c} if isinstance(c, str) else c for c in cols
        ]
    elif cols is not None:
        out["columns"] = cols

    for key in ("filters", "sort"):
        if key in out:
            out[key] = _maybe_json(out.get(key))

    gb = _maybe_json(out.get("group_by"))
    if isinstance(gb, str):
        gb = [g.strip() for g in re.split(r"[;,\n]", gb) if g.strip()]
    if gb is not None:
        out["group_by"] = gb

    lim = out.get("limit")
    if isinstance(lim, str):
        try:
            out["limit"] = int(lim.strip())
        except ValueError:
            out.pop("limit", None)

    return out


def _effective_spec(payload: "SpecTableRequest") -> dict[str, Any] | None:
    """Reconstruct a spec dict from whatever shape the model produced."""
    raw = payload.spec
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = None
    if not isinstance(raw, dict):
        # Flattened form: assemble from recognised top-level fields.
        assembled: dict[str, Any] = {}
        for key in ("source", "columns", "filters", "sort", "group_by", "limit"):
            val = getattr(payload, key, None)
            if val is not None:
                assembled[key] = val
        raw = assembled or None
    return _coerce_spec_fields(raw) if isinstance(raw, dict) else None


class SpecTablePatchRequest(BaseModel):
    canvas_id: str = DEFAULT_CANVAS
    # Either explicit ops, or a natural-language command (deterministic parser).
    ops: list[PatchOp] | None = None
    command: str | None = None


class SpecTableResponse(BaseModel):
    status: str
    canvas_id: str
    total: int
    shown: int
    message: str
    truncated: bool = False
    spec: dict[str, Any] | None = None
    filters: dict[str, Any] = {}


async def _render_and_publish(
    db: AsyncSession, canvas_id: str, spec: TableSpec, *, note: str = ""
) -> SpecTableResponse:
    try:
        result = await execute_spec(db, spec)
    except ValueError as exc:
        return SpecTableResponse(
            status="error", canvas_id=canvas_id, total=0, shown=0,
            message=f"Неверная спецификация: {exc}",
        )

    title = spec.title or SOURCES[spec.source].title
    block = {
        "id": canvas_id,
        "type": "table",
        "title": title,
        "columns": result.columns,
        "rows": result.rows,
        "total_rows": result.total,
        "truncated": result.truncated,
        "spec": spec.model_dump(mode="json"),
        "source": "workspace.spec_table",
    }
    stored = upsert_workspace_block(canvas_id, block)
    await chat_bus.publish({
        "type": "workspace.updated",
        "canvas_id": canvas_id,
        "block": stored,
    })
    suffix = f" (показаны первые {len(result.rows)})" if result.truncated else ""
    note_part = f"{note}. " if note else ""
    return SpecTableResponse(
        status="published",
        canvas_id=canvas_id,
        total=result.total,
        shown=len(result.rows),
        message=(
            f"{note_part}Таблица «{title}»: {result.total} строк — полные данные из БД{suffix}."
        ),
        truncated=result.truncated,
        spec=spec.model_dump(mode="json"),
    )


@router.post("/agent/spec-table", response_model=SpecTableResponse)
async def publish_spec_table(
    payload: SpecTableRequest,
    db: AsyncSession = Depends(get_db),
) -> SpecTableResponse:
    """Skill: workspace.spec_table — Build a table from a declarative spec.

    Spec format: {source: invoices|invoice_items|suppliers, columns:
    [{field, header?}], filters: [{field, op, value}], sort: [{field, dir}]}.
    Allowed fields per source — see /api/workspace/agent/spec-table/catalog.
    The result ALWAYS contains the full dataset (true total, hard cap 5000).
    """
    sources = ", ".join(sorted(SOURCES))
    effective = _effective_spec(payload)
    if not effective:
        return SpecTableResponse(
            status="error", canvas_id=payload.canvas_id, total=0, shown=0,
            message=(
                "Не передана спецификация. Передай spec как объект: "
                f"{{source: {sources}, columns: [{{field, header?}}], "
                "filters: [{field, op, value}], sort: [{field, dir}]}. "
                "Справочник полей: action=spec_table_catalog."
            ),
        )
    try:
        spec = TableSpec.model_validate(effective)
    except Exception as exc:
        return SpecTableResponse(
            status="error", canvas_id=payload.canvas_id, total=0, shown=0,
            message=(
                f"Спецификация не разобрана: {str(exc)[:300]}. "
                f"Формат: {{source: {sources}, columns: [{{field, header?}}], "
                "filters: [{field, op, value}], sort: [{field, dir}]}. "
                "Справочник полей: action=spec_table_catalog."
            ),
        )
    if payload.title:
        spec = spec.model_copy(update={"title": payload.title})
    problems = validate_spec(spec)
    if problems:
        return SpecTableResponse(
            status="error", canvas_id=payload.canvas_id, total=0, shown=0,
            message=(
                "Неверная спецификация: " + "; ".join(problems)
                + ". Справочник полей: action=spec_table_catalog."
            ),
        )
    return await _render_and_publish(db, payload.canvas_id, spec)


@router.post("/agent/spec-table/patch", response_model=SpecTableResponse)
async def patch_spec_table(
    payload: SpecTablePatchRequest,
    db: AsyncSession = Depends(get_db),
) -> SpecTableResponse:
    """Skill: workspace.spec_table_patch — Edit an existing spec table.

    Accepts explicit patch ops (add_column/remove_column/move_column/set_sort/
    add_filter/clear_filters) or a Russian command («добавь столбец с НДС перед
    суммой», «отсортируй по сумме по убыванию», «покажи только фрезы …»).
    """
    block = get_workspace_block(payload.canvas_id)
    if not block or not isinstance(block.get("spec"), dict):
        return SpecTableResponse(
            status="error", canvas_id=payload.canvas_id, total=0, shown=0,
            message=(
                "Блок не найден или не является spec-таблицей — "
                "сначала постройте таблицу через workspace.spec_table."
            ),
        )
    spec = TableSpec.model_validate(block["spec"])

    ops = list(payload.ops or [])
    note = ""
    if not ops and payload.command:
        parsed = parse_patch_command(payload.command, spec)
        if parsed is None:
            return SpecTableResponse(
                status="unrecognized", canvas_id=payload.canvas_id,
                total=0, shown=0,
                message=(
                    "Команда не распознана детерминированно — сформируйте ops "
                    "явно (add_column/remove_column/set_sort/add_filter)."
                ),
                spec=spec.model_dump(mode="json"),
            )
        ops = parsed.ops
        note = parsed.description
    if not ops:
        return SpecTableResponse(
            status="error", canvas_id=payload.canvas_id, total=0, shown=0,
            message="Не передано ни ops, ни command.",
        )

    try:
        new_spec = apply_patch(spec, ops)
    except ValueError as exc:
        return SpecTableResponse(
            status="error", canvas_id=payload.canvas_id, total=0, shown=0,
            message=f"Невозможно применить правку: {exc}",
            spec=spec.model_dump(mode="json"),
        )
    return await _render_and_publish(db, payload.canvas_id, new_spec, note=note)


class SpecTableCellEditRequest(BaseModel):
    canvas_id: str = DEFAULT_CANVAS
    row_pk: str
    field: str
    value: Any = None
    requested_by: str | None = None


class SpecTableCellEditResponse(BaseModel):
    status: str
    message: str
    approval_id: str | None = None


@router.post("/agent/spec-table/cell-edit", response_model=SpecTableCellEditResponse)
async def edit_spec_table_cell(
    payload: SpecTableCellEditRequest,
    db: AsyncSession = Depends(get_db),
) -> SpecTableCellEditResponse:
    """Skill: workspace.spec_table_cell_edit — Queue a cell edit for approval.

    Draft-first: the edit is NOT written to the DB here. It files a DraftAction
    routed through the ``table.apply_diff`` approval gate; the change is applied
    only once a human approves it (see approvals._execute_approved_action).
    """
    block = get_workspace_block(payload.canvas_id)
    if not block or not isinstance(block.get("spec"), dict):
        return SpecTableCellEditResponse(
            status="error",
            message="Блок не найден или не является spec-таблицей.",
        )
    source_key = str(block["spec"].get("source", ""))
    wb = writeback_for(source_key)
    if wb is None:
        return SpecTableCellEditResponse(
            status="error",
            message=f"Источник «{source_key}» доступен только для чтения.",
        )
    if payload.field not in wb.editable:
        return SpecTableCellEditResponse(
            status="error",
            message=(
                f"Поле «{payload.field}» нередактируемо. "
                f"Редактируемые: {', '.join(sorted(wb.editable))}."
            ),
        )
    try:
        entity_id = uuid.UUID(str(payload.row_pk))
    except (ValueError, TypeError):
        return SpecTableCellEditResponse(
            status="error", message="Некорректный идентификатор строки."
        )

    approval = Approval(
        action_type=ApprovalActionType.table_apply_diff,
        entity_type=wb.entity_type,
        entity_id=entity_id,
        requested_by=payload.requested_by or "sveta",
        context={
            "source": source_key,
            "field": payload.field,
            "value": payload.value,
            "title": block.get("title"),
        },
    )
    db.add(approval)
    await db.flush()

    draft = DraftAction(
        action_type="table.apply_diff",
        entity_type=wb.entity_type,
        entity_id=entity_id,
        draft_data={
            "source": source_key,
            "field": payload.field,
            "value": payload.value,
        },
        approval_id=approval.id,
    )
    db.add(draft)
    await db.commit()

    return SpecTableCellEditResponse(
        status="pending_approval",
        message=(
            f"Правка поля «{payload.field}» отправлена на подтверждение."
        ),
        approval_id=str(approval.id),
    )


@router.get("/agent/spec-table/catalog")
async def spec_table_catalog() -> dict[str, Any]:
    """Field catalog: sources and their allowed fields (for the agent/UI)."""
    return {
        "_spec_format": {
            "source": "invoices|invoice_items|suppliers|…",
            "columns": "[{field, header?}]",
            "filters": "[{field, op, value}] — op: eq|ne|contains|gte|lte|between|in|smart",
            "sort": "[{field, dir: asc|desc}]",
            "group_by": "[field] — объединить/сгруппировать строки по полю "
                        "(«объедини по поставщикам» → group_by:['supplier_name']); "
                        "строки кластеризуются, sort применяется внутри группы",
            "limit": "int | null (все строки)",
        },
        "sources": {
            source.key: {
                "title": source.title,
                "default_columns": list(source.default_columns),
                "fields": [
                    {
                        "key": fd.key,
                        "header": fd.header,
                        "type": fd.type,
                        "synonyms": list(fd.synonyms),
                    }
                    for fd in source.fields.values()
                ],
            }
            for source in SOURCES.values()
        },
    }
