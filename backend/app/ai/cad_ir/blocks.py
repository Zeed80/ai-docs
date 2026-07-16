"""A2: reusable blocks — define a named block from selected geometry and stamp
instances anywhere on the sheet.

The block stores deep copies of the source entities plus a base point (their
bbox centre). Insertion translates (and optionally rotates) fresh copies so
they land with the base at the clicked point — the inserted entities are
ordinary IR entities, so validation, rendering and every export path work on
them unchanged.
"""

from __future__ import annotations

from app.ai.cad_ir.schema import BlockDef, CadIR, Entity, Point
from app.ai.cad_ir.transform import SketchOpError, duplicate_entity, rotate_entity


def _entity_points(entity: Entity) -> list[Point]:
    pts: list[Point] = []
    for attr in ("p1", "p2", "center", "position"):
        val = getattr(entity, attr, None)
        if val is not None:
            pts.append(val)
    pts.extend(getattr(entity, "points", None) or [])
    pts.extend(getattr(entity, "boundary", None) or [])
    return pts


def define_block(ir: CadIR, name: str, entity_ids: list[str]) -> BlockDef:
    """Snapshot the given entities as a named block (originals stay in place).
    Redefining an existing name replaces it."""
    name = name.strip()
    if not name:
        raise SketchOpError("у блока должно быть имя")
    entities = [e for e in ir.entities if e.id in set(entity_ids)]
    if not entities:
        raise SketchOpError("не выбрана геометрия для блока")
    pts = [p for e in entities for p in _entity_points(e)]
    if not pts:
        raise SketchOpError("выбранные сущности не содержат опорных точек")
    base = Point(
        x=(min(p.x for p in pts) + max(p.x for p in pts)) / 2,
        y=(min(p.y for p in pts) + max(p.y for p in pts)) / 2,
    )
    block = BlockDef(name=name, base=base, entities=[e.model_copy(deep=True) for e in entities])
    ir.blocks = [b for b in ir.blocks if b.name != name] + [block]
    return block


def insert_block(
    ir: CadIR, name: str, x: float, y: float, rotation_deg: float = 0.0
) -> list[Entity]:
    """Stamp one instance of the named block with its base point at (x, y),
    optionally rotated about the insertion point. Returns the new entities
    (already appended to ``ir.entities``)."""
    block = next((b for b in ir.blocks if b.name == name.strip()), None)
    if block is None:
        raise SketchOpError(f"блок {name!r} не найден")
    dx, dy = x - block.base.x, y - block.base.y
    out: list[Entity] = []
    for entity in block.entities:
        copy = duplicate_entity(entity, dx, dy)
        if abs(rotation_deg) > 1e-9:
            copy = rotate_entity(copy, Point(x=x, y=y), rotation_deg)
        out.append(copy)
    ir.entities.extend(out)
    return out
