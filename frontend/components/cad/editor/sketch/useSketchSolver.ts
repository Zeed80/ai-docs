"use client";

import { useEffect, useState } from "react";

import {
  evaluateSketch,
  type SketchConstraintCheck,
  type SketchDofReport,
} from "@/lib/cad-sketch-api";
import type { IrConstraint, IrEntity } from "@/lib/studio-api";

/** Ф4: live DOF/violated feedback as the sketch changes, WITHOUT solving —
 * mirrors ConstraintsPanel.tsx's own refreshChecks, just debounced against
 * local entity/constraint state instead of re-fetched per generation. A
 * failed probe (e.g. mid-edit malformed constraint) clears silently, same
 * "a status probe must not break the panel" rule ConstraintsPanel follows. */
export function useSketchSolver(
  entities: IrEntity[],
  constraints: IrConstraint[],
) {
  const [checks, setChecks] = useState<SketchConstraintCheck[]>([]);
  const [dof, setDof] = useState<SketchDofReport | null>(null);

  useEffect(() => {
    if (constraints.length === 0 || entities.length === 0) {
      setChecks([]);
      setDof(null);
      return;
    }
    const handle = window.setTimeout(() => {
      evaluateSketch(entities, constraints)
        .then((res) => {
          setChecks(res.checks);
          setDof(res.dof);
        })
        .catch(() => {
          setChecks([]);
          setDof(null);
        });
    }, 400);
    return () => window.clearTimeout(handle);
  }, [entities, constraints]);

  return { checks, dof };
}
