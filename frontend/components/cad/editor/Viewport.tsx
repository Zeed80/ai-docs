"use client";

import CadModelViewer from "@/components/studio/CadModelViewer";
import type { OperationBounds } from "@/lib/emg-tree";

/** Тонкая обёртка над CadModelViewer (сам вьювер не изменён — B2/B3 уже
 * дали ему всё, что нужно этому редактору) + легенда рамок. Вынесено из
 * CadEditorShell отдельным компонентом, чтобы Тело/Эскиз могли добавлять
 * свои view-контролы рядом, не раздувая шелл. */
export default function Viewport({
  hasModel,
  modelUrl,
  topologyUrl,
  operationBounds,
  flaggedOperationIds,
  selectedOperationId,
  onOperationClick,
}: {
  hasModel: boolean;
  modelUrl: string;
  topologyUrl: string | undefined;
  operationBounds: Map<string, OperationBounds>;
  flaggedOperationIds: Set<string>;
  selectedOperationId: string | null;
  onOperationClick: (operationId: string | null) => void;
}) {
  return (
    <div className="relative min-h-0 flex-1">
      {hasModel && flaggedOperationIds.size > 0 && (
        <div className="pointer-events-none absolute left-2 top-2 z-10 flex items-center gap-3 rounded bg-zinc-950/70 px-2 py-1 text-[10px] text-zinc-300">
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm border border-amber-400 bg-amber-400/40" />
            требует проверки
          </span>
          <span className="flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm border border-sky-400 bg-sky-400/40" />
            выбрано
          </span>
        </div>
      )}
      {hasModel ? (
        <CadModelViewer
          url={modelUrl}
          topologyUrl={topologyUrl}
          loadingLabel="Загрузка модели…"
          errorLabel="Не удалось загрузить модель"
          operationBounds={operationBounds}
          flaggedOperationIds={flaggedOperationIds}
          selectedOperationId={selectedOperationId}
          onOperationClick={onOperationClick}
        />
      ) : (
        <div className="grid h-full place-items-center text-sm text-zinc-600">
          3D-модель ещё не построена
        </div>
      )}
    </div>
  );
}
