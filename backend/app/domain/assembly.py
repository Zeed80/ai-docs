"""Deterministic assembly instance, mate and degrees-of-freedom analysis."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_MATE_DOF_REMOVAL = {
    "fixed": 6,
    "coincident": 3,
    "concentric": 4,
    "distance": 1,
    "angle": 1,
    "parallel": 2,
    "perpendicular": 1,
    "tangent": 1,
    "gear": 1,
}


class AssemblyDofReport(BaseModel):
    active_instances: list[str] = Field(default_factory=list)
    grounded_instances: list[str] = Field(default_factory=list)
    floating_instances: list[str] = Field(default_factory=list)
    grouped_quantity_instances: list[str] = Field(default_factory=list)
    invalid_mates: list[str] = Field(default_factory=list)
    unsupported_mates: list[str] = Field(default_factory=list)
    constraint_rank_estimate: int = 0
    degrees_of_freedom: int = 0
    overconstrained: bool = False
    fully_constrained: bool = False


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def analyze_assembly_dof(
    components: list[Any],
    mates: list[Any],
) -> AssemblyDofReport:
    """Estimate rigid-body DOF from declared mate semantics without geometry guesses.

    This is a rank accounting gate, not a geometric constraint solver. Unknown
    mate types remain explicit and block ``fully_constrained``.
    """
    active = [item for item in components if not bool(_value(item, "suppressed", False))]
    keys = [str(_value(item, "instance_key")) for item in active]
    key_set = set(keys)
    grounded = sorted(
        str(_value(item, "instance_key"))
        for item in active
        if bool((_value(item, "metadata_", {}) or {}).get("grounded"))
    )
    grouped = sorted(
        str(_value(item, "instance_key"))
        for item in active
        if int(_value(item, "quantity", 1) or 1) != 1
    )
    parent = {key: key for key in keys}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(first: str, second: str) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[second_root] = first_root

    invalid = []
    unsupported = []
    removal = 0
    for index, mate in enumerate(mates):
        mate_id = str(_value(mate, "id", f"mate:{index}"))
        first = str(_value(mate, "first_instance_key", ""))
        second = str(_value(mate, "second_instance_key", ""))
        if first not in key_set or second not in key_set or first == second:
            invalid.append(mate_id)
            continue
        mate_type = str(_value(mate, "mate_type", "")).lower()
        parameters = _value(mate, "parameters", {}) or {}
        declared = parameters.get("dof_removed")
        if isinstance(declared, int) and not isinstance(declared, bool) and 1 <= declared <= 6:
            removed = declared
        else:
            removed = _MATE_DOF_REMOVAL.get(mate_type)
        if removed is None:
            unsupported.append(mate_id)
            continue
        removal += removed
        union(first, second)

    ground_roots = {find(key) for key in grounded}
    floating = sorted(key for key in keys if find(key) not in ground_roots)
    base_dof = 6 * len(keys)
    grounded_removal = 6 * len(grounded)
    available_after_ground = max(0, base_dof - grounded_removal)
    rank = min(removal, available_after_ground)
    overconstrained = removal > available_after_ground
    remaining = max(0, available_after_ground - rank)
    fully_constrained = bool(keys) and not any((
        remaining,
        floating,
        grouped,
        invalid,
        unsupported,
        overconstrained,
    ))
    return AssemblyDofReport(
        active_instances=keys,
        grounded_instances=grounded,
        floating_instances=floating,
        grouped_quantity_instances=grouped,
        invalid_mates=invalid,
        unsupported_mates=unsupported,
        constraint_rank_estimate=rank,
        degrees_of_freedom=remaining,
        overconstrained=overconstrained,
        fully_constrained=fully_constrained,
    )
