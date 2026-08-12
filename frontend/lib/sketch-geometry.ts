/** Ф4 нового CAD-редактора: pure geometry helpers shared by the whole-sheet
 * constraint editor (ConstraintsPanel.tsx, operates on a persisted CadIr)
 * and the scoped sketch canvas (editor/sketch/SketchCanvas.tsx,
 * operates on a small in-memory entity list) — extracted here rather than
 * duplicated, since both work over the exact same IrEntity/constraint
 * shapes and the exact same "which point on this entity can a constraint
 * reference" rules. No React, no fetch — safe to import from either. */

import type { IrEntity } from "@/lib/studio-api";

export type PointRef = { entity_id: string; point: "p1" | "p2" | "center" };

export type ConstraintKind =
  | "horizontal"
  | "vertical"
  | "radius"
  | "diameter"
  | "parallel"
  | "perpendicular"
  | "angle"
  | "equal"
  | "concentric"
  | "distance"
  | "coincident";

export const CONSTRAINT_NEEDS_VALUE: ReadonlySet<ConstraintKind> = new Set([
  "radius",
  "diameter",
  "angle",
  "distance",
]);

/** Endpoints/centre a constraint can reference on a given entity. */
export function refPoints(e: IrEntity): PointRef[] {
  if (e.type === "segment")
    return [
      { entity_id: e.id, point: "p1" },
      { entity_id: e.id, point: "p2" },
    ];
  if (e.type === "circle" || e.type === "arc")
    return [{ entity_id: e.id, point: "center" }];
  return [];
}

export function coord(
  e: IrEntity,
  ref: PointRef,
): { x: number; y: number } | null {
  if (ref.point === "center" && e.center) return e.center;
  if (ref.point === "p1" && e.p1) return e.p1;
  if (ref.point === "p2" && e.p2) return e.p2;
  return null;
}

/** The closest pair of reference points between two entities — what a user
 * means by "make these coincident / this far apart" when they pick two
 * shapes. */
export function nearestRefs(
  a: IrEntity,
  b: IrEntity,
): [PointRef, PointRef] | null {
  let best: [PointRef, PointRef] | null = null;
  let bestD = Infinity;
  for (const ra of refPoints(a)) {
    const pa = coord(a, ra);
    if (!pa) continue;
    for (const rb of refPoints(b)) {
      const pb = coord(b, rb);
      if (!pb) continue;
      const d = Math.hypot(pa.x - pb.x, pa.y - pb.y);
      if (d < bestD) {
        bestD = d;
        best = [ra, rb];
      }
    }
  }
  return best;
}

/** Which constraints the current selection admits (arity + entity types) —
 * mirrors constraints.py's own supported-kind set exactly (see its
 * evaluate_constraints), since offering a kind the solver can't actually
 * evaluate would just always fail with "недостаточно ссылок". */
export function availableConstraints(sel: IrEntity[]): ConstraintKind[] {
  if (sel.length === 1) {
    const e = sel[0];
    if (e.type === "segment") return ["horizontal", "vertical"];
    if (e.type === "circle" || e.type === "arc") return ["radius", "diameter"];
    return [];
  }
  if (sel.length === 2) {
    const segs = sel.filter((e) => e.type === "segment").length;
    const circ = sel.filter(
      (e) => e.type === "circle" || e.type === "arc",
    ).length;
    const kinds: ConstraintKind[] = ["coincident", "distance"];
    if (segs === 2) kinds.push("parallel", "perpendicular", "angle", "equal");
    if (circ === 2) kinds.push("concentric", "equal");
    return kinds;
  }
  return [];
}
