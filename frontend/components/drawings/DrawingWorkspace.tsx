"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import type {
  Drawing,
  DrawingFeature,
  DrawingWithFeatures,
} from "@/lib/drawings-api";
import { drawingsApi } from "@/lib/drawings-api";
import { DrawingViewer } from "./DrawingViewer";
import { FeatureTree } from "./FeatureTree";
import { FeatureEditor } from "./FeatureEditor";
import { ToolBindingPanel } from "./ToolBindingPanel";

type RightPanel = "viewer" | "editor";
type BottomPanel = "tool" | "none";

interface DrawingWorkspaceProps {
  drawingId: string;
}

export function DrawingWorkspace({ drawingId }: DrawingWorkspaceProps) {
  const [drawing, setDrawing] = useState<DrawingWithFeatures | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedFeatureId, setSelectedFeatureId] = useState<string | null>(
    null,
  );
  const [hoveredFeatureId, setHoveredFeatureId] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [editingFeature, setEditingFeature] = useState<DrawingFeature | null>(
    null,
  );
  const [rightPanel, setRightPanel] = useState<RightPanel>("viewer");
  const [showToolPanel, setShowToolPanel] = useState(false);
  const [reanalyzing, setReanalyzing] = useState(false);
  const pollingRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await drawingsApi.get(drawingId);
      setDrawing(data);
      setError(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Ошибка загрузки");
    } finally {
      setLoading(false);
    }
  }, [drawingId]);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while analyzing
  useEffect(() => {
    if (!drawing) return;
    if (drawing.status === "analyzing" || drawing.status === "uploaded") {
      pollingRef.current = setInterval(load, 3000);
    } else {
      if (pollingRef.current) clearInterval(pollingRef.current);
    }
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [drawing?.status, load]);

  const selectedFeature =
    drawing?.features.find((f) => f.id === selectedFeatureId) ?? null;

  const handleFeatureSelect = (featureId: string) => {
    setSelectedFeatureId(featureId);
    setShowToolPanel(true);
  };

  const handleEditFeature = (feature: DrawingFeature) => {
    setEditingFeature(feature);
    setRightPanel("editor");
    setEditMode(true);
  };

  const handleSaveContours = async (
    contours: Parameters<typeof drawingsApi.updateContours>[2],
  ) => {
    if (!editingFeature) return;
    await drawingsApi.updateContours(drawingId, editingFeature.id, contours);
    await load();
    setRightPanel("viewer");
    setEditMode(false);
    setEditingFeature(null);
  };

  const handleCancelEdit = () => {
    setRightPanel("viewer");
    setEditMode(false);
    setEditingFeature(null);
  };

  const handleReanalyze = async () => {
    setReanalyzing(true);
    try {
      await drawingsApi.reanalyze(drawingId);
      await load();
    } finally {
      setReanalyzing(false);
    }
  };

  const svgUrl = drawingId ? drawingsApi.getSvgUrl(drawingId) : null;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-white/40 gap-3">
        <div className="w-6 h-6 border-2 border-blue-500/40 border-t-blue-500 rounded-full animate-spin" />
        <span>Загрузка чертежа...</span>
      </div>
    );
  }

  if (error || !drawing) {
    return (
      <div className="flex items-center justify-center h-full text-red-400 gap-2">
        <span>⚠</span>
        <span>{error || "Чертёж не найден"}</span>
      </div>
    );
  }

  const titleBlock = drawing.title_block as Record<string, string> | null;

  return (
    <div className="flex flex-col h-full bg-zinc-950 text-white overflow-hidden">
      {/* Top toolbar */}
      <div className="flex items-center gap-3 px-4 py-2 bg-zinc-900 border-b border-white/10 shrink-0">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-sm truncate">
              {titleBlock?.title || drawing.filename}
            </span>
            {titleBlock?.drawing_number && (
              <span className="text-white/40 text-xs font-mono">
                {titleBlock.drawing_number}
              </span>
            )}
            {drawing.revision && (
              <span className="bg-amber-600/20 text-amber-400 text-xs px-1.5 py-0.5 rounded font-mono">
                Ред. {drawing.revision}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3 mt-0.5">
            <StatusBadge status={drawing.status} />
            {titleBlock?.material && (
              <span className="text-white/40 text-xs">
                <span className="text-white/20">Материал: </span>
                {titleBlock.material}
              </span>
            )}
            {titleBlock?.scale && (
              <span className="text-white/40 text-xs">
                <span className="text-white/20">Масштаб: </span>
                {titleBlock.scale}
              </span>
            )}
            <span className="text-white/30 text-xs">
              {drawing.features.length} элем.
            </span>
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          {/* Edit mode toggle */}
          <button
            onClick={() => {
              if (editMode) {
                setEditMode(false);
                setRightPanel("viewer");
                setEditingFeature(null);
              } else {
                setEditMode(true);
              }
            }}
            className={clsx(
              "flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition-colors",
              editMode
                ? "bg-amber-600/20 text-amber-400 border border-amber-600/40"
                : "bg-zinc-800 hover:bg-zinc-700 text-white/70 hover:text-white",
            )}
          >
            <span>✏</span>
            <span>{editMode ? "Выйти из редактора" : "Редактировать"}</span>
          </button>

          {/* Tool panel toggle */}
          <button
            onClick={() => setShowToolPanel((s) => !s)}
            className={clsx(
              "flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition-colors",
              showToolPanel
                ? "bg-blue-600/20 text-blue-400 border border-blue-600/40"
                : "bg-zinc-800 hover:bg-zinc-700 text-white/70 hover:text-white",
            )}
          >
            🔧 Инструменты
          </button>

          {/* Reanalyze */}
          <button
            onClick={handleReanalyze}
            disabled={reanalyzing || drawing.status === "analyzing"}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs bg-zinc-800 hover:bg-zinc-700 text-white/70 hover:text-white disabled:opacity-40 transition-colors"
            title="Повторный AI-анализ"
          >
            {reanalyzing ? (
              <span className="w-3 h-3 border border-white/40 border-t-white rounded-full animate-spin inline-block" />
            ) : (
              "⚡"
            )}
            Анализ
          </button>
        </div>
      </div>

      {/* Main content */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* Left panel: Feature tree */}
        <div className="w-72 shrink-0 bg-zinc-900 border-r border-white/10 flex flex-col overflow-hidden">
          <div className="px-3 py-2 border-b border-white/10 text-xs text-white/40 uppercase tracking-wider font-medium">
            Элементы чертежа
          </div>
          <div className="flex-1 overflow-y-auto">
            <FeatureTree
              features={drawing.features}
              selectedFeatureId={selectedFeatureId}
              hoveredFeatureId={hoveredFeatureId}
              onSelect={handleFeatureSelect}
              onHover={setHoveredFeatureId}
              editMode={editMode}
              onEditFeature={handleEditFeature}
            />
          </div>
        </div>

        {/* Center: Drawing viewer or editor */}
        <div className="flex-1 flex flex-col min-w-0 overflow-hidden p-2">
          {rightPanel === "viewer" && (
            <DrawingViewer
              drawingId={drawingId}
              svgUrl={svgUrl}
              features={drawing.features}
              selectedFeatureId={selectedFeatureId}
              hoveredFeatureId={hoveredFeatureId}
              onFeatureClick={handleFeatureSelect}
              onFeatureHover={setHoveredFeatureId}
              editMode={editMode}
              editingFeatureId={editingFeature?.id}
            />
          )}

          {rightPanel === "editor" && editingFeature && (
            <div className="flex-1 bg-zinc-900 rounded-lg border border-amber-500/30 p-3 overflow-hidden flex flex-col">
              <div className="mb-2 flex items-center gap-2">
                <span className="text-amber-400 text-xs font-medium">
                  ✏ Редактирование: {editingFeature.name}
                </span>
              </div>
              <FeatureEditor
                feature={editingFeature}
                svgViewBox={
                  drawing.bounding_box
                    ? {
                        x:
                          (drawing.bounding_box as Record<string, number>)
                            .x_min ?? 0,
                        y:
                          (drawing.bounding_box as Record<string, number>)
                            .y_min ?? 0,
                        width:
                          (drawing.bounding_box as Record<string, number>)
                            .x_max ?? 200,
                        height:
                          (drawing.bounding_box as Record<string, number>)
                            .y_max ?? 150,
                      }
                    : undefined
                }
                onSave={handleSaveContours}
                onCancel={handleCancelEdit}
              />
            </div>
          )}
        </div>

        {/* Right panel: Tool binding */}
        {showToolPanel && (
          <div className="w-64 shrink-0 bg-zinc-900 border-l border-white/10 overflow-hidden flex flex-col">
            <ToolBindingPanel
              drawingId={drawingId}
              feature={selectedFeature}
              onBindingChanged={load}
            />
          </div>
        )}
      </div>

      {/* Status bar */}
      {drawing.status === "analyzing" && (
        <div className="shrink-0 px-4 py-1.5 bg-blue-900/30 border-t border-blue-500/20 flex items-center gap-2 text-xs text-blue-300">
          <div className="w-3 h-3 border border-blue-400/50 border-t-blue-400 rounded-full animate-spin" />
          Анализ чертежа... Ожидайте результатов
        </div>
      )}
      {drawing.status === "failed" && drawing.analysis_error && (
        <div className="shrink-0 px-4 py-1.5 bg-red-900/30 border-t border-red-500/20 text-xs text-red-300">
          Ошибка анализа: {drawing.analysis_error}
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const config: Record<string, { label: string; cls: string }> = {
    uploaded: { label: "Загружен", cls: "text-zinc-400 bg-zinc-700/50" },
    analyzing: {
      label: "Анализ...",
      cls: "text-blue-400 bg-blue-500/15 animate-pulse",
    },
    analyzed: {
      label: "Проанализирован",
      cls: "text-green-400 bg-green-500/15",
    },
    needs_review: {
      label: "На проверке",
      cls: "text-yellow-400 bg-yellow-500/15",
    },
    approved: { label: "Утверждён", cls: "text-emerald-400 bg-emerald-500/15" },
    failed: { label: "Ошибка", cls: "text-red-400 bg-red-500/15" },
  };
  const c = config[status] || {
    label: status,
    cls: "text-zinc-400 bg-zinc-700/50",
  };
  return (
    <span className={clsx("text-xs px-1.5 py-0.5 rounded font-medium", c.cls)}>
      {c.label}
    </span>
  );
}
