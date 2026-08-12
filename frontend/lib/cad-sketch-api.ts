"use client";

/** Ф4 нового CAD-редактора: client for the 3 stateless sketch-solver
 * endpoints (backend/app/api/cad_sketch.py) — no generation_id, no
 * persisted revision, exactly the scratch-session scope SketchCanvas.tsx
 * needs. Mirrors evaluateConstraints's own request pattern in
 * studio-api.ts, just against a different base path. */

import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";
import type { IrCadParameter, IrConstraint, IrEntity } from "@/lib/studio-api";

const BASE = `${getApiBaseUrl()}/api/cad-sketch`;

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export interface SketchConstraintCheck {
  constraint_id: string;
  ok: boolean;
  message: string;
  entity_ids: string[];
}

export interface SketchDofReport {
  dof: number;
  unknowns: number;
  equations: number;
  rank: number;
  state:
    | "unconstrained"
    | "under_constrained"
    | "well_constrained"
    | "over_constrained";
  redundant: boolean;
  conflict: boolean;
}

export async function solveSketch(
  entities: IrEntity[],
  constraints: IrConstraint[],
  parameters: IrCadParameter[] = [],
  maxNfev = 200,
): Promise<{
  entities: IrEntity[];
  converged: boolean;
  residual: number;
  iterations: number;
  message: string;
  checks: SketchConstraintCheck[];
}> {
  const res = await mutFetch(`${BASE}/solve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      entities,
      constraints,
      parameters,
      max_nfev: maxNfev,
    }),
  });
  return jsonOrThrow(res);
}

export async function evaluateSketch(
  entities: IrEntity[],
  constraints: IrConstraint[],
): Promise<{
  checks: SketchConstraintCheck[];
  violated: number;
  dof: SketchDofReport;
}> {
  const res = await mutFetch(`${BASE}/evaluate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entities, constraints }),
  });
  return jsonOrThrow(res);
}

export interface SketchProfileSegment {
  kind: "line" | "arc";
  // [x, y] pairs -- the kernel's own _sketch_point (infra/cad-kernel/
  // server.py) strictly requires a 2-element list, not an {x,y} object.
  to: [number, number];
  center?: [number, number];
  clockwise?: boolean;
}

export async function exportSketchProfile(
  entities: IrEntity[],
): Promise<{ sketch_profile: SketchProfileSegment[] }> {
  const res = await mutFetch(`${BASE}/export-profile`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entities }),
  });
  return jsonOrThrow(res);
}
