"use client";

import { useMemo, useState } from "react";

import type { EmgOperationNode } from "@/lib/emg-tree";
import { operationForAssumption } from "@/lib/emg-tree";

/** solid_3d.assumptions, made actionable: each note that names a stable
 * Feature id becomes a button that selects the operation realizing it in
 * the tree — the same information the AssurancePanel further up the
 * (non-editor) page shows as plain text, but here one click from the fix. */
export default function AssumptionsStrip({
  assumptions,
  operations,
  onSelectOperation,
}: {
  assumptions: string[];
  operations: EmgOperationNode[];
  onSelectOperation: (id: string) => void;
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
            return (
              <li key={`${index}-${item.slice(0, 30)}`}>
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
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
