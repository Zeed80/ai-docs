"use client";

import { useState } from "react";

import CadModelViewer from "@/components/studio/CadModelViewer";
import type { CadViewCommand } from "@/components/studio/CadModelViewer";
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
  edgePickActive,
  onEdgeSelect,
}: {
  hasModel: boolean;
  modelUrl: string;
  topologyUrl: string | undefined;
  operationBounds: Map<string, OperationBounds>;
  flaggedOperationIds: Set<string>;
  selectedOperationId: string | null;
  onOperationClick: (operationId: string | null) => void;
  // Ф7: true while the "Добавить фичу" form is open on fillet/chamfer —
  // shows a hint and activates edge click-selection; the model stays
  // clickable for operation bounds either way, both raycasts are
  // independent (see CadModelViewer's own onClick).
  edgePickActive?: boolean;
  onEdgeSelect?: (edgeKey: string | null) => void;
}) {
  const [viewCommand, setViewCommand] = useState<CadViewCommand>({
    type: "fit_model",
    nonce: 0,
  });
  const runViewCommand = (type: CadViewCommand["type"]) =>
    setViewCommand((current) => ({ type, nonce: current.nonce + 1 }));
  const canFitSelection = Boolean(
    selectedOperationId && operationBounds.has(selectedOperationId),
  );

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
      {hasModel && edgePickActive && (
        <div className="pointer-events-none absolute right-2 top-2 z-10 rounded bg-sky-500/15 px-2 py-1 text-[10px] text-sky-200">
          Кликните по ребру на модели, чтобы выбрать его
        </div>
      )}
      {hasModel && (
        <div
          className="absolute bottom-3 right-3 z-10 flex items-center gap-1 rounded border border-white/10 bg-zinc-950/80 p-1 shadow-lg"
          aria-label="Масштаб 3D-вида"
        >
          <button
            type="button"
            aria-label="Приблизить 3D-модель"
            title="Приблизить"
            onClick={() => runViewCommand("zoom_in")}
            className="grid h-7 w-7 place-items-center rounded text-sm text-zinc-300 hover:bg-white/10 hover:text-white"
          >
            +
          </button>
          <button
            type="button"
            aria-label="Отдалить 3D-модель"
            title="Отдалить"
            onClick={() => runViewCommand("zoom_out")}
            className="grid h-7 w-7 place-items-center rounded text-sm text-zinc-300 hover:bg-white/10 hover:text-white"
          >
            −
          </button>
          <button
            type="button"
            aria-label="Показать всю 3D-модель"
            title="Вписать всю модель"
            onClick={() => runViewCommand("fit_model")}
            className="h-7 rounded px-2 text-[10px] text-zinc-300 hover:bg-white/10 hover:text-white"
          >
            Вся
          </button>
          <button
            type="button"
            aria-label="Приблизить выбранную операцию"
            title={
              canFitSelection
                ? "Вписать точный kernel bbox выбранной операции"
                : "Для операции нет измеренного kernel bbox"
            }
            disabled={!canFitSelection}
            onClick={() => runViewCommand("fit_selection")}
            className="h-7 rounded px-2 text-[10px] text-sky-200 hover:bg-sky-500/15 disabled:cursor-not-allowed disabled:text-zinc-600"
          >
            Выбор
          </button>
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
          onEdgeSelect={onEdgeSelect}
          // This wrapper is already a properly-bounded flex-1 pane (see its
          // own className above) — CadModelViewer's DEFAULT fixed preview
          // height (built for CadWorkspace.tsx's unbounded review-page
          // embed) wasted roughly half the viewport as an empty dark gap
          // here instead of filling it. Live user report: "занято пол
          // экрана... непонятный интерфейс".
          heightClassName="h-full"
          viewCommand={viewCommand}
        />
      ) : (
        <div className="grid h-full place-items-center text-sm text-zinc-600">
          3D-модель ещё не построена
        </div>
      )}
    </div>
  );
}
