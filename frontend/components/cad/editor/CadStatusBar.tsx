"use client";

import { kindLabel, type EmgOperationNode } from "@/lib/emg-tree";

/** Ф10: a persistent bottom strip — the editor's own version of the
 * whole-sheet drawing editor's StatusBar.tsx (components/cad/StatusBar.tsx),
 * which isn't reusable here as-is: it's built around OSNAP/ORTHO toggles
 * and mm-scaled cursor coordinates, concepts this 3D feature-tree editor
 * doesn't have. Same idea (always-visible state at a glance), different
 * content: which operation is selected, how many need review, build
 * status. */
export default function CadStatusBar({
  selectedOperation,
  operationCount,
  needsReviewCount,
  hasModel,
}: {
  selectedOperation: EmgOperationNode | null;
  operationCount: number;
  needsReviewCount: number;
  hasModel: boolean;
}) {
  return (
    <div className="flex shrink-0 items-center gap-3 border-t border-white/10 bg-zinc-900/80 px-3 py-1 text-[11px] text-zinc-500">
      <span>
        {selectedOperation
          ? `Выбрано: ${kindLabel(selectedOperation.kind)}`
          : "Ничего не выбрано"}
      </span>
      <span className="text-zinc-700">·</span>
      <span>Операций: {operationCount}</span>
      {needsReviewCount > 0 && (
        <>
          <span className="text-zinc-700">·</span>
          <span className="text-amber-400">
            Требует проверки: {needsReviewCount}
          </span>
        </>
      )}
      <span className="flex-1" />
      <span className={hasModel ? "text-emerald-400" : "text-zinc-600"}>
        {hasModel ? "Модель построена" : "Модель не построена"}
      </span>
    </div>
  );
}
