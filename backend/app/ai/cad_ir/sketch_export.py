"""Ф4 нового CAD-редактора: a solved sketch's entities -> the kernel's own
sketch-profile wire format.

The kernel's ``_sketch_wire`` (infra/cad-kernel/server.py) already consumes
an ORDERED chain of ``{kind: "line"|"arc", to: [x,y], center?: [x,y],
clockwise?}`` segments (``_sketch_point`` requires a 2-element LIST, not an
``{x,y}`` object) walking a closed loop from an ALWAYS-(0.0, 0.0) implicit
start point — every call site (base profile AND boss/pocket/sweep/loft
tools alike) hardcodes ``start=(0.0, 0.0)``; the profile's own coordinates
must already be relative to whichever vertex the walk begins at, the kernel
never translates. This module is the other end of that bridge: given a
flat, unordered set of Segment/Arc CadIR entities (what a browser sketch
canvas naturally produces, in the canvas's OWN absolute drawing
coordinates), verify they form exactly one simple closed loop, walk it, and
RE-BASE every emitted point so the walk's own start vertex becomes exactly
``(0, 0)`` — the offset is the walk's start vertex itself, subtracted from
every ``to``/``center`` this module emits. Never invents a vertex, a
connection, or a direction the entities themselves don't state — a loop
that doesn't close, branches, or leaves entities outside the main cycle is
a 422, not a best-effort guess.

Arc convention (this module's own, since a sketch built here has no
source drawing to inherit one from): ``start_angle -> end_angle`` sweeping
counter-clockwise (increasing angle) is the arc's "natural" direction —
walking it forward emits ``clockwise: false``, walking it backward (from
the end_angle end to the start_angle end) emits ``clockwise: true``. Kept
private to this module; nothing else needs to agree with it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from app.ai.cad_ir.schema import Arc, Circle, Entity, Point, Segment

_VERTEX_TOLERANCE_MM = 1e-4


def _vertex_key(point: Point) -> tuple[float, float]:
    return (
        round(point.x / _VERTEX_TOLERANCE_MM) * _VERTEX_TOLERANCE_MM,
        round(point.y / _VERTEX_TOLERANCE_MM) * _VERTEX_TOLERANCE_MM,
    )


def _arc_endpoints(arc: Arc) -> tuple[Point, Point]:
    """The two points an Arc's own (center, radius, start_angle, end_angle)
    already fully determine — never a value computed any other way."""
    start = Point(
        x=arc.center.x + arc.radius * math.cos(math.radians(arc.start_angle)),
        y=arc.center.y + arc.radius * math.sin(math.radians(arc.start_angle)),
    )
    end = Point(
        x=arc.center.x + arc.radius * math.cos(math.radians(arc.end_angle)),
        y=arc.center.y + arc.radius * math.sin(math.radians(arc.end_angle)),
    )
    return start, end


def cad_ir_profile_to_sketch_segments(entities: list[Entity]) -> list[dict[str, Any]]:
    """Walk a flat set of Segment/Arc entities into the kernel's ordered
    sketch-profile wire format. Raises ``ValueError`` (the caller turns
    this into a 422) when the entities are not exactly one simple closed
    loop — never approximates a near-miss into a "close enough" wire."""
    if not entities:
        raise ValueError("Эскиз пуст")

    # The kernel wire has no circle opcode, but two exact semicircular arcs
    # are lossless and form the same closed profile.  A standalone circle is
    # the only valid simple loop containing a Circle entity; mixing it with
    # other disconnected contour entities remains a fail-closed error below.
    if len(entities) == 1 and isinstance(entities[0], Circle):
        circle = entities[0]
        radius = circle.radius
        return [
            {
                "kind": "arc",
                "to": [-2.0 * radius, 0.0],
                "center": [-radius, 0.0],
                "clockwise": False,
            },
            {
                "kind": "arc",
                "to": [0.0, 0.0],
                "center": [-radius, 0.0],
                "clockwise": False,
            },
        ]

    chain: list[tuple[str, Entity, Point, Point]] = []
    for entity in entities:
        if isinstance(entity, Segment):
            chain.append(("line", entity, entity.p1, entity.p2))
        elif isinstance(entity, Arc):
            start, end = _arc_endpoints(entity)
            chain.append(("arc", entity, start, end))
        else:
            raise ValueError(
                f"Эскиз-профиль может содержать линии, дуги или одну окружность, не '{entity.type}'"
            )

    adjacency: dict[tuple[float, float], list[int]] = defaultdict(list)
    for index, (_, _, a, b) in enumerate(chain):
        adjacency[_vertex_key(a)].append(index)
        adjacency[_vertex_key(b)].append(index)
    bad_vertices = {key: len(items) for key, items in adjacency.items() if len(items) != 2}
    if bad_vertices:
        raise ValueError(
            "Контур не замкнут или разветвлён: в "
            f"{len(bad_vertices)} точке(ах) сходится не ровно 2 элемента "
            "(проверьте, что все линии/дуги образуют один замкнутый контур)"
        )

    _kind0, _entity0, a0, _b0 = chain[0]
    start_vertex = _vertex_key(a0)
    # The kernel's _sketch_wire always walks from an implicit (0, 0) — every
    # point this function emits must be relative to the walk's own start
    # vertex (a0's actual drawn coordinates), not the sketch canvas's
    # absolute frame.
    origin_x, origin_y = a0.x, a0.y
    current_vertex = start_vertex
    current_index = 0
    visited: set[int] = set()
    order: list[dict[str, Any]] = []

    while True:
        kind, entity, pa, pb = chain[current_index]
        forward = _vertex_key(pa) == current_vertex
        to_point = pb if forward else pa
        if kind == "line":
            order.append(
                {
                    "kind": "line",
                    "to": [to_point.x - origin_x, to_point.y - origin_y],
                }
            )
        else:
            arc_start, _arc_end = _arc_endpoints(entity)
            # The loop-walk direction and the arc's OWN stored direction
            # (start_angle -> end_angle) can differ — re-derive which one
            # we are walking away from.
            frm_point = pa if forward else pb
            arc_forward = _vertex_key(frm_point) == _vertex_key(arc_start)
            order.append(
                {
                    "kind": "arc",
                    "to": [to_point.x - origin_x, to_point.y - origin_y],
                    "center": [entity.center.x - origin_x, entity.center.y - origin_y],
                    "clockwise": not arc_forward,
                }
            )
        visited.add(current_index)
        current_vertex = _vertex_key(to_point)
        if current_vertex == start_vertex:
            break
        candidates = [i for i in adjacency[current_vertex] if i not in visited]
        if len(candidates) != 1:
            raise ValueError(
                "Контур не образует единственный простой замкнутый цикл "
                f"(в точке {current_vertex} нет ровно одного непройденного продолжения)"
            )
        current_index = candidates[0]

    if len(visited) != len(chain):
        raise ValueError(
            f"В эскизе есть элементы вне основного замкнутого контура "
            f"({len(chain) - len(visited)} из {len(chain)} не вошли в цикл)"
        )
    return order
