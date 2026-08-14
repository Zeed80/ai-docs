"use client";

import { useMemo, useState } from "react";

import type { EmgOperationNode } from "@/lib/emg-tree";
import { operationForAssumption } from "@/lib/emg-tree";

export type EdgeRepairTarget = { kind: "chamfer" | "fillet"; index: number };

// C: cad_solid.py's _edge_features embeds the array index precisely so this
// can be parsed back out — "chamfer[2]: не удалось определить ребро
// (shoulder) — не построен". An unplaceable chamfer/fillet never becomes a
// BuildOperation at all (see _edge_selector), so there is no tree row to
// select — this note is the ONLY handle on it, and until now it was inert
// text like any other assumption.
const UNPLACEABLE_EDGE =
  /^(chamfer|fillet)\[(\d+)\]: не удалось определить ребро/;

function parseEdgeRepairTarget(item: string): EdgeRepairTarget | null {
  const match = item.match(UNPLACEABLE_EDGE);
  if (!match) return null;
  return { kind: match[1] as "chamfer" | "fillet", index: Number(match[2]) };
}

/** solid_3d.assumptions, made actionable: each note that names a stable
 * Feature id becomes a button that selects the operation realizing it in
 * the tree — the same information the AssurancePanel further up the
 * (non-editor) page shows as plain text, but here one click from the fix.
 * An unplaceable chamfer/fillet edge (no operation exists to select) gets
 * its own repair affordance instead: click the real edge on the model. */
export default function AssumptionsStrip({
  assumptions,
  operations,
  onSelectOperation,
  onRepairEdge,
}: {
  assumptions: string[];
  operations: EmgOperationNode[];
  onSelectOperation: (id: string) => void;
  onRepairEdge: (target: EdgeRepairTarget) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const visible = useMemo(
    () => assumptions.filter((item) => !item.startsWith("critical assertion ")),
    [assumptions],
  );
  if (visible.length === 0) return null;

  return (
    <div className="shrink-0 border-t border-amber-500/30 bg-amber-500/5">
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[11px] font-medium text-amber-300"
      >
        <span>{collapsed ? "▸" : "▾"}</span>
        Требует проверки: {visible.length}
      </button>
      {!collapsed && (
        <ul className="max-h-32 space-y-1 overflow-y-auto px-3 pb-2 text-[11px]">
          {visible.map((item, index) => {
            const operation = operationForAssumption(operations, item);
            const edgeTarget = operation ? null : parseEdgeRepairTarget(item);
            return (
              <li
                key={`${index}-${item.slice(0, 30)}`}
                className="flex flex-wrap items-center gap-x-2"
              >
                {operation ? (
                  <button
                    type="button"
                    onClick={() => onSelectOperation(operation.id)}
                    className="text-left text-amber-100 underline decoration-amber-500/40 decoration-dotted hover:text-amber-50"
                  >
                    {item}
                  </button>
                ) : (
                  <span className="text-amber-200/80">{item}</span>
                )}
                {edgeTarget && (
                  <button
                    type="button"
                    onClick={() => onRepairEdge(edgeTarget)}
                    className="rounded border border-sky-400/40 px-1.5 py-0.5 text-sky-200 hover:bg-sky-400/10"
                  >
                    Указать ребро на модели
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
