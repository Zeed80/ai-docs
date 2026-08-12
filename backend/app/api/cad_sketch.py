"""Ф4 нового CAD-редактора (/root/.claude/plans/starry-mapping-hippo.md):
a scoped, ephemeral 2D sketch session — draw/constrain one feature's
profile, live in the browser only, nothing persisted server-side until the
solved result is submitted as a new feature (POST .../model-graph/features
with profile: "sketch").

All three endpoints are stateless: no generation_id, no DB row, no
cad_ir_store revision. They wrap the SAME solver
(app.ai.cad_ir.constraints) the whole-sheet /{id}/ir/solve and
/{id}/ir/constraints/evaluate endpoints already use — reused unchanged,
just fed a small in-memory CadIR built from the request body instead of a
stored, persisted-per-keystroke document. See constraints.py's own
docstring: it is explicitly document-agnostic (CadIR only requires
`source`, everything else defaults), so a scratch sketch is already a
first-class input, not a special case.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.ai.cad_ir.schema import CadParameter, Entity, GeometricConstraint, SourceInfo
from app.auth.jwt import get_current_user
from app.auth.models import UserInfo

router = APIRouter()

# A scratch sketch has no source image to size a coordinate system against —
# SourceInfo.image_width/height only need to satisfy their own gt=0, nothing
# downstream in constraints.py or sketch_export.py ever reads them.
_BLANK_SOURCE = SourceInfo(kind="blank", image_width=1, image_height=1)


def _build_ir(
    entities: list[Entity],
    constraints: list[GeometricConstraint],
    parameters: list[CadParameter],
):
    from app.ai.cad_ir.schema import CadIR

    return CadIR(
        source=_BLANK_SOURCE,
        entities=entities,
        constraints=constraints,
        parameters=parameters,
    )


class SketchSolveRequest(BaseModel):
    entities: list[Entity] = Field(min_length=1, max_length=500)
    constraints: list[GeometricConstraint] = Field(default_factory=list, max_length=500)
    parameters: list[CadParameter] = Field(default_factory=list, max_length=100)
    max_nfev: int = Field(default=200, ge=1, le=2000)


@router.post("/solve")
async def solve_sketch(
    body: SketchSolveRequest,
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Numerically satisfy the sketch's own constraints — moves entity
    coordinates, never adds/removes geometry or constraints. Same solver
    the whole-sheet /{id}/ir/solve uses (solve_constraints), unmodified."""
    from app.ai.cad_ir.constraints import solve_constraints

    ir = _build_ir(body.entities, body.constraints, body.parameters)
    result = solve_constraints(ir, max_nfev=body.max_nfev)
    return {
        "entities": [entity.model_dump(mode="json") for entity in ir.entities],
        "converged": result.converged,
        "residual": result.residual,
        "iterations": result.iterations,
        "message": result.message,
        "checks": [
            {
                "constraint_id": c.constraint_id,
                "ok": c.ok,
                "message": c.message,
                "entity_ids": list(c.entity_ids),
            }
            for c in result.checks
        ],
    }


class SketchEvaluateRequest(BaseModel):
    entities: list[Entity] = Field(min_length=1, max_length=500)
    constraints: list[GeometricConstraint] = Field(default_factory=list, max_length=500)
    parameters: list[CadParameter] = Field(default_factory=list, max_length=100)


@router.post("/evaluate")
async def evaluate_sketch(
    body: SketchEvaluateRequest,
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Per-constraint satisfaction + degrees-of-freedom, WITHOUT solving —
    same shape GET /{id}/ir/constraints/evaluate already returns, same
    functions (evaluate_constraints/analyze_constraints), unmodified."""
    from app.ai.cad_ir.constraints import analyze_constraints, evaluate_constraints

    ir = _build_ir(body.entities, body.constraints, body.parameters)
    checks = evaluate_constraints(ir)
    dof = analyze_constraints(ir)
    return {
        "checks": [
            {
                "constraint_id": c.constraint_id,
                "ok": c.ok,
                "message": c.message,
                "entity_ids": list(c.entity_ids),
            }
            for c in checks
        ],
        "violated": sum(1 for c in checks if not c.ok),
        "dof": {
            "dof": dof.dof,
            "unknowns": dof.unknowns,
            "equations": dof.equations,
            "rank": dof.rank,
            "state": dof.state,
            "redundant": dof.redundant,
            "conflict": dof.conflict,
        },
    }


class SketchExportRequest(BaseModel):
    entities: list[Entity] = Field(min_length=1, max_length=500)


@router.post("/export-profile")
async def export_sketch_profile(
    body: SketchExportRequest,
    user: UserInfo = Depends(get_current_user),
) -> dict[str, Any]:
    """Walk an already-solved (or hand-placed) closed loop of Segment/Arc
    entities into the kernel's own sketch_profile wire format — see
    cad_ir_profile_to_sketch_segments's own docstring for exactly what it
    accepts and rejects. 422, never a best-effort guess, when the entities
    are not exactly one simple closed loop."""
    from app.ai.cad_ir.sketch_export import cad_ir_profile_to_sketch_segments

    try:
        profile = cad_ir_profile_to_sketch_segments(body.entities)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"sketch_profile": profile}
