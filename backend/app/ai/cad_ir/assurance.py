"""Assurance-ladder transition rules.

A model's high confidence is not engineering correctness — trust is raised
only by the actor entitled to that rung:

    recognizer (model/pipeline)  → observed | inferred (initial states only)
    solver / deterministic check → constraint_validated | calculation_validated
    human                        → human_approved

Downgrades are always allowed (new evidence may invalidate old trust —
e.g. a diffusion-modified region demotes to inferred). A recognizer can
never touch an entity a human already approved.
"""

from __future__ import annotations

from app.ai.cad_ir.schema import Assurance, Entity

Actor = str  # "recognizer" | "solver" | "human"

_LADDER: dict[Assurance, int] = {
    "observed": 0,
    "inferred": 0,
    "constraint_validated": 1,
    "calculation_validated": 2,
    "human_approved": 3,
}

_ACTOR_CEILING: dict[Actor, int] = {
    "recognizer": 0,
    "solver": 2,
    "human": 3,
}


class AssuranceTransitionError(ValueError):
    pass


def can_set(actor: Actor, current: Assurance, new: Assurance) -> bool:
    ceiling = _ACTOR_CEILING.get(actor)
    if ceiling is None:
        return False
    if _LADDER[new] > ceiling:
        return False
    # Nobody except the human touches human-approved state.
    if current == "human_approved" and actor != "human":
        return False
    return True


def set_assurance(entity: Entity, new: Assurance, actor: Actor) -> None:
    """Apply a transition or raise. Use everywhere instead of direct writes."""
    if not can_set(actor, entity.assurance, new):
        raise AssuranceTransitionError(
            f"actor={actor!r} не может перевести assurance "
            f"{entity.assurance!r} → {new!r} (элемент {entity.id})"
        )
    entity.assurance = new


def sanitize_incoming(payload: dict, actor: Actor) -> dict:
    """Strip assurance the caller has no right to claim from an entity payload
    (PATCH add/update). Human edits become human_approved; anything else
    enters at the bottom of the ladder."""
    payload = dict(payload)
    claimed = payload.get("assurance")
    if actor == "human":
        payload["assurance"] = "human_approved"
    elif claimed is not None and _LADDER.get(claimed, 99) > _ACTOR_CEILING.get(actor, 0):
        payload["assurance"] = "inferred"
    return payload
