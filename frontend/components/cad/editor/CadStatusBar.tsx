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
  incrementalBuild,
}: {
  selectedOperation: EmgOperationNode | null;
  operationCount: number;
  needsReviewCount: number;
  hasModel: boolean;
  incrementalBuild?: {
    cache_enabled?: boolean;
    body_count?: number;
    cache_hit_body_indices?: number[];
    reused_feature_indices?: number[];
    rebuilt_feature_indices?: number[];
    full_rebuild?: boolean;
  };
}) {
  const reusedBodies = incrementalBuild?.cache_hit_body_indices?.length ?? 0;
  const reusedOperations = incrementalBuild?.reused_feature_indices?.length ?? 0;
  const rebuiltOperations = incrementalBuild?.rebuilt_feature_indices?.length ?? 0;
  return (
    <div className="flex shrink-0 items-center gap-3 border-t border-white/10 bg-zinc-900/80 px-3 py-1 text-[11px] text-zinc-400">
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
      {hasModel && incrementalBuild && (
        <span
          className={reusedOperations > 0 ? "text-sky-300" : "text-zinc-400"}
          title={
            reusedOperations > 0
              ? "Переиспользован только самый длинный BREP-reopen и topology-validated префикс фактического порядка операций"
              : "Все операции прошли через OpenCascade без переиспользования checkpoint"
          }
        >
          {reusedOperations > 0
            ? `Переиспользовано операций: ${reusedOperations}; пересобрано: ${rebuiltOperations}`
            : reusedBodies > 0
              ? `Переиспользовано тел: ${reusedBodies}/${incrementalBuild.body_count ?? "?"}`
              : "Все операции пересобраны"}
        </span>
      )}
      {hasModel && incrementalBuild && (
        <span className="text-zinc-700">·</span>
      )}
      <span className={hasModel ? "text-emerald-400" : "text-zinc-400"}>
        {hasModel ? "Модель построена" : "Модель не построена"}
      </span>
    </div>
  );
}
