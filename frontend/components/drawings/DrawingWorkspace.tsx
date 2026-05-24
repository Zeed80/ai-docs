"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import clsx from "clsx";
import type {
  Drawing,
  DrawingFeature,
  DrawingViewSection,
  AssemblyBOMItem,
  DrawingValidationReport,
  DrawingWithFeatures,
  ReanalyzeOptions,
  FeatureCorrectionPayload,
} from "@/lib/drawings-api";
import { drawingsApi } from "@/lib/drawings-api";
import { DrawingViewer } from "./DrawingViewer";
import { FeatureTree } from "./FeatureTree";
import { FeatureEditor } from "./FeatureEditor";
import { ToolBindingPanel } from "./ToolBindingPanel";

type RightPanel = "viewer" | "editor";
type LeftTab = "features" | "views" | "bom" | "validation" | "review";

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
  const [leftTab, setLeftTab] = useState<LeftTab>("features");

  const [views, setViews] = useState<DrawingViewSection[] | null>(null);
  const [bom, setBom] = useState<AssemblyBOMItem[] | null>(null);
  const [validation, setValidation] = useState<DrawingValidationReport | null>(
    null,
  );
  const [uncertainFeatures, setUncertainFeatures] = useState<
    DrawingFeature[] | null
  >(null);
  const [reviewingFeature, setReviewingFeature] =
    useState<DrawingFeature | null>(null);
  const [showReviewPanel, setShowReviewPanel] = useState(false);
  const [correcting, setCorrecting] = useState(false);

  const [showReanalyzeDialog, setShowReanalyzeDialog] = useState(false);
  const [reanalyzing, setReanalyzing] = useState(false);
  const [reanalyzeOpts, setReanalyzeOpts] = useState<ReanalyzeOptions>({
    allow_cloud: false,
    max_views: 6,
    force_drawing_type: undefined,
  });

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

  const loadViews = useCallback(async () => {
    try {
      const data = await drawingsApi.getViews(drawingId);
      setViews(data);
    } catch {
      setViews([]);
    }
  }, [drawingId]);

  const loadBOM = useCallback(async () => {
    try {
      const data = await drawingsApi.getAssemblyBOM(drawingId);
      setBom(data);
    } catch {
      setBom([]);
    }
  }, [drawingId]);

  const loadValidation = useCallback(async () => {
    try {
      const data = await drawingsApi.getValidation(drawingId);
      setValidation(data);
    } catch {
      setValidation(null);
    }
  }, [drawingId]);

  const loadUncertainFeatures = useCallback(async () => {
    try {
      const data = await drawingsApi.getUncertainFeatures(drawingId);
      setUncertainFeatures(data);
    } catch {
      setUncertainFeatures([]);
    }
  }, [drawingId]);

  useEffect(() => {
    if (leftTab === "views" && views === null) loadViews();
    if (leftTab === "bom" && bom === null) loadBOM();
    if (leftTab === "validation" && validation === null) loadValidation();
    if (leftTab === "review" && uncertainFeatures === null)
      loadUncertainFeatures();
  }, [
    leftTab,
    views,
    bom,
    validation,
    uncertainFeatures,
    loadViews,
    loadBOM,
    loadValidation,
    loadUncertainFeatures,
  ]);

  const handleOpenReview = (feature: DrawingFeature) => {
    setReviewingFeature(feature);
    setSelectedFeatureId(feature.id);
    setShowReviewPanel(true);
    setShowToolPanel(false);
  };

  const handleCorrectFeature = async (payload: FeatureCorrectionPayload) => {
    if (!reviewingFeature) return;
    setCorrecting(true);
    try {
      const updated = await drawingsApi.correctFeature(
        drawingId,
        reviewingFeature.id,
        payload,
      );
      // Update in main feature list
      setDrawing((prev) =>
        prev
          ? {
              ...prev,
              features: prev.features.map((f) =>
                f.id === updated.id ? updated : f,
              ),
            }
          : prev,
      );
      // Remove from uncertain list and advance to next
      setUncertainFeatures((prev) => {
        if (!prev) return prev;
        const remaining = prev.filter((f) => f.id !== reviewingFeature.id);
        const nextFeature = remaining[0] ?? null;
        if (nextFeature) {
          setReviewingFeature(nextFeature);
          setSelectedFeatureId(nextFeature.id);
        } else {
          setReviewingFeature(null);
          setShowReviewPanel(false);
        }
        return remaining;
      });
    } finally {
      setCorrecting(false);
    }
  };

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
    setShowReanalyzeDialog(false);
    try {
      await drawingsApi.reanalyze(drawingId, reanalyzeOpts);
      setViews(null);
      setBom(null);
      setValidation(null);
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
            {drawing.is_confidential && (
              <span className="bg-red-900/30 text-red-400 text-xs px-1.5 py-0.5 rounded border border-red-800/40">
                Конф.
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

          <button
            onClick={() => setShowReanalyzeDialog(true)}
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

          {drawing.status === "analyzed" && (
            <Link
              href={`/technology/new?drawing_id=${drawingId}`}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs bg-emerald-700/80 hover:bg-emerald-600 text-white font-medium transition-colors"
              title="Создать технологический процесс из этого чертежа"
            >
              ⚙ Создать ТП
            </Link>
          )}
        </div>
      </div>

      {/* Main content */}
      <div className="flex flex-1 min-h-0 overflow-hidden">
        {/* Left panel: tabbed */}
        <div className="w-72 shrink-0 bg-zinc-900 border-r border-white/10 flex flex-col overflow-hidden">
          {/* Tab bar */}
          <div className="flex border-b border-white/10 shrink-0">
            {(
              [
                { id: "features", label: "Элементы" },
                { id: "views", label: "Виды" },
                { id: "bom", label: "Спец." },
                { id: "validation", label: "Валид." },
              ] as { id: LeftTab; label: string }[]
            ).map((tab) => (
              <button
                key={tab.id}
                onClick={() => setLeftTab(tab.id)}
                className={clsx(
                  "flex-1 text-xs py-2 px-1 font-medium transition-colors truncate",
                  leftTab === tab.id
                    ? "text-blue-400 border-b-2 border-blue-500 bg-blue-500/5"
                    : "text-white/40 hover:text-white/70 border-b-2 border-transparent",
                )}
              >
                {tab.label}
              </button>
            ))}
            <button
              onClick={() => setLeftTab("review")}
              className={clsx(
                "flex-1 text-xs py-2 px-1 font-medium transition-colors",
                leftTab === "review"
                  ? "text-amber-400 border-b-2 border-amber-500 bg-amber-500/5"
                  : "text-white/40 hover:text-white/70 border-b-2 border-transparent",
              )}
            >
              Пров.
              {uncertainFeatures && uncertainFeatures.length > 0 && (
                <span className="ml-1 text-xs bg-amber-600 text-white rounded-full px-1 py-px font-bold">
                  {uncertainFeatures.length}
                </span>
              )}
            </button>
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-y-auto">
            {leftTab === "features" && (
              <FeatureTree
                features={drawing.features}
                selectedFeatureId={selectedFeatureId}
                hoveredFeatureId={hoveredFeatureId}
                onSelect={handleFeatureSelect}
                onHover={setHoveredFeatureId}
                editMode={editMode}
                onEditFeature={handleEditFeature}
              />
            )}
            {leftTab === "views" && <ViewsPanel views={views} />}
            {leftTab === "bom" && <AssemblyBOMPanel items={bom} />}
            {leftTab === "validation" && (
              <ValidationPanel
                report={validation}
                drawingStatus={drawing.status}
              />
            )}
            {leftTab === "review" && (
              <ReviewListPanel
                features={uncertainFeatures}
                reviewingId={reviewingFeature?.id ?? null}
                onSelect={handleOpenReview}
              />
            )}
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

        {/* Right panel: Review OR Tool binding */}
        {showReviewPanel && reviewingFeature ? (
          <div className="w-72 shrink-0 bg-zinc-900 border-l border-amber-500/30 overflow-hidden flex flex-col">
            <ReviewPanel
              feature={reviewingFeature}
              total={
                uncertainFeatures
                  ? (uncertainFeatures.length ?? 0) +
                    (uncertainFeatures.find((f) => f.id === reviewingFeature.id)
                      ? 1
                      : 0)
                  : 1
              }
              remaining={uncertainFeatures?.length ?? 0}
              correcting={correcting}
              onCorrect={handleCorrectFeature}
              onSkip={() => {
                const next =
                  uncertainFeatures?.find(
                    (f) => f.id !== reviewingFeature.id,
                  ) ?? null;
                if (next) {
                  setReviewingFeature(next);
                  setSelectedFeatureId(next.id);
                } else {
                  setShowReviewPanel(false);
                  setReviewingFeature(null);
                }
              }}
              onClose={() => {
                setShowReviewPanel(false);
                setReviewingFeature(null);
              }}
            />
          </div>
        ) : (
          showToolPanel && (
            <div className="w-64 shrink-0 bg-zinc-900 border-l border-white/10 overflow-hidden flex flex-col">
              <ToolBindingPanel
                drawingId={drawingId}
                feature={selectedFeature}
                onBindingChanged={load}
              />
            </div>
          )
        )}
      </div>

      {/* Status bar */}
      {drawing.status === "analyzing" && (
        <div className="shrink-0 px-4 py-1.5 bg-blue-900/30 border-t border-blue-500/20 flex items-center gap-2 text-xs text-blue-300">
          <div className="w-3 h-3 border border-blue-400/50 border-t-blue-400 rounded-full animate-spin" />
          Анализ чертежа... Ожидайте результатов
        </div>
      )}
      {drawing.status === "needs_review" && (
        <div className="shrink-0 px-4 py-1.5 bg-yellow-900/20 border-t border-yellow-500/20 flex items-center gap-2 text-xs text-yellow-300">
          <span>⚠</span>
          Низкая уверенность — требуется проверка. Откройте вкладку «Валидация»
          для деталей.
        </div>
      )}
      {drawing.status === "failed" && drawing.analysis_error && (
        <div className="shrink-0 px-4 py-1.5 bg-red-900/30 border-t border-red-500/20 text-xs text-red-300">
          Ошибка анализа: {drawing.analysis_error}
        </div>
      )}

      {/* Reanalyze dialog */}
      {showReanalyzeDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
          <div className="bg-zinc-900 border border-white/10 rounded-xl p-5 w-96 shadow-2xl">
            <h3 className="text-sm font-semibold text-white mb-4">
              Параметры повторного анализа
            </h3>

            <div className="space-y-3">
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={reanalyzeOpts.allow_cloud ?? false}
                  onChange={(e) =>
                    setReanalyzeOpts((o) => ({
                      ...o,
                      allow_cloud: e.target.checked,
                    }))
                  }
                  className="w-4 h-4 accent-blue-500"
                />
                <span className="text-xs text-white/80">
                  Разрешить облачные модели
                  {drawing.is_confidential && (
                    <span className="ml-1 text-red-400">
                      (чертёж конфиденциален)
                    </span>
                  )}
                </span>
              </label>

              <div>
                <label className="block text-xs text-white/50 mb-1">
                  Макс. видов: {reanalyzeOpts.max_views}
                </label>
                <input
                  type="range"
                  min={1}
                  max={12}
                  value={reanalyzeOpts.max_views ?? 6}
                  onChange={(e) =>
                    setReanalyzeOpts((o) => ({
                      ...o,
                      max_views: Number(e.target.value),
                    }))
                  }
                  className="w-full accent-blue-500"
                />
              </div>

              <div>
                <label className="block text-xs text-white/50 mb-1">
                  Тип чертежа (авто если не указан)
                </label>
                <select
                  value={reanalyzeOpts.force_drawing_type ?? ""}
                  onChange={(e) =>
                    setReanalyzeOpts((o) => ({
                      ...o,
                      force_drawing_type: (e.target.value ||
                        undefined) as ReanalyzeOptions["force_drawing_type"],
                    }))
                  }
                  className="w-full bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-xs text-white"
                >
                  <option value="">Авто-определение</option>
                  <option value="detail">Деталь</option>
                  <option value="assembly">Сборка</option>
                  <option value="section">Разрез / Сечение</option>
                  <option value="weld">Сварная конструкция</option>
                </select>
              </div>
            </div>

            <div className="flex gap-2 mt-5">
              <button
                onClick={handleReanalyze}
                className="flex-1 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs font-medium transition-colors"
              >
                Запустить анализ
              </button>
              <button
                onClick={() => setShowReanalyzeDialog(false)}
                className="flex-1 py-2 rounded bg-zinc-800 hover:bg-zinc-700 text-white/70 text-xs transition-colors"
              >
                Отмена
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Sub-panels ────────────────────────────────────────────────────────────────

function ViewsPanel({ views }: { views: DrawingViewSection[] | null }) {
  if (views === null) {
    return (
      <div className="flex items-center justify-center h-24 text-white/30 text-xs">
        <div className="w-4 h-4 border border-white/20 border-t-white/50 rounded-full animate-spin mr-2" />
        Загрузка...
      </div>
    );
  }
  if (views.length === 0) {
    return (
      <div className="px-3 py-4 text-white/30 text-xs text-center">
        Виды не выделены.
        <br />
        Запустите повторный анализ для мультивидового распознавания.
      </div>
    );
  }

  const sectionTypeLabel: Record<string, string> = {
    front: "Главный вид",
    side: "Вид сбоку",
    top: "Вид сверху",
    section: "Разрез",
    isometric: "Изометрия",
    detail: "Выносной элемент",
  };

  return (
    <div className="divide-y divide-white/5">
      {views.map((v) => (
        <div
          key={v.id}
          className="px-3 py-2.5 hover:bg-white/5 transition-colors"
        >
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-white/80 font-medium truncate">
              {v.section_label ||
                sectionTypeLabel[v.section_type] ||
                v.section_type}
            </span>
            <span className="text-xs text-white/30 shrink-0">
              {Math.round(v.confidence * 100)}%
            </span>
          </div>
          <div className="flex items-center gap-2 mt-0.5">
            <span className="text-xs text-white/40">
              {sectionTypeLabel[v.section_type] || v.section_type}
            </span>
            {v.cutting_plane_label && (
              <span className="text-xs text-blue-400 font-mono">
                {v.cutting_plane_label}
              </span>
            )}
            {v.page_number && v.page_number > 1 && (
              <span className="text-xs text-white/25">
                стр. {v.page_number}
              </span>
            )}
          </div>
          {v.bbox_on_sheet && (
            <div className="text-xs text-white/20 font-mono mt-0.5">
              {Math.round(v.bbox_on_sheet.x ?? 0)},
              {Math.round(v.bbox_on_sheet.y ?? 0)}{" "}
              {Math.round(v.bbox_on_sheet.w ?? 0)}×
              {Math.round(v.bbox_on_sheet.h ?? 0)}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function AssemblyBOMPanel({ items }: { items: AssemblyBOMItem[] | null }) {
  if (items === null) {
    return (
      <div className="flex items-center justify-center h-24 text-white/30 text-xs">
        <div className="w-4 h-4 border border-white/20 border-t-white/50 rounded-full animate-spin mr-2" />
        Загрузка...
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="px-3 py-4 text-white/30 text-xs text-center">
        Спецификация не найдена.
        <br />
        Применимо только к сборочным чертежам.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="text-white/30 border-b border-white/10">
            <th className="text-left px-2 py-1.5 font-medium w-8">#</th>
            <th className="text-left px-2 py-1.5 font-medium">Наименование</th>
            <th className="text-right px-2 py-1.5 font-medium w-10">Кол.</th>
            <th className="text-left px-2 py-1.5 font-medium">Материал</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-white/5">
          {items.map((item) => (
            <tr key={item.id} className="hover:bg-white/5 transition-colors">
              <td className="px-2 py-1.5 text-white/40 font-mono">
                {item.item_no}
              </td>
              <td className="px-2 py-1.5">
                <div
                  className="text-white/80 truncate max-w-[120px]"
                  title={item.designation}
                >
                  {item.designation}
                </div>
                {item.drawing_number && (
                  <div className="text-white/30 font-mono text-xs">
                    {item.drawing_number}
                  </div>
                )}
              </td>
              <td className="px-2 py-1.5 text-right text-white/70">
                {item.quantity} {item.unit !== "шт" ? item.unit : ""}
              </td>
              <td
                className="px-2 py-1.5 text-white/40 truncate max-w-[80px]"
                title={item.material}
              >
                {item.material || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ValidationPanel({
  report,
  drawingStatus,
}: {
  report: DrawingValidationReport | null;
  drawingStatus: string;
}) {
  if (report === null) {
    return (
      <div className="flex items-center justify-center h-24 text-white/30 text-xs">
        <div className="w-4 h-4 border border-white/20 border-t-white/50 rounded-full animate-spin mr-2" />
        Загрузка...
      </div>
    );
  }

  if (
    report.status === "not_validated" ||
    report.confidence_score === undefined
  ) {
    return (
      <div className="px-3 py-4 text-white/30 text-xs text-center">
        {report.message || "Валидация ещё не выполнена."}
        <br />
        Запустите повторный анализ для получения отчёта.
      </div>
    );
  }

  const score = report.confidence_score ?? 0;
  const scoreColor =
    score >= 0.8
      ? "text-green-400"
      : score >= 0.6
        ? "text-yellow-400"
        : "text-red-400";
  const scoreBg =
    score >= 0.8
      ? "bg-green-500"
      : score >= 0.6
        ? "bg-yellow-500"
        : "bg-red-500";

  return (
    <div className="px-3 py-3 space-y-3">
      {/* Score */}
      <div>
        <div className="flex items-center justify-between mb-1">
          <span className="text-xs text-white/50">Уверенность</span>
          <span className={clsx("text-sm font-bold tabular-nums", scoreColor)}>
            {Math.round(score * 100)}%
          </span>
        </div>
        <div className="h-1.5 bg-zinc-800 rounded-full overflow-hidden">
          <div
            className={clsx("h-full rounded-full transition-all", scoreBg)}
            style={{ width: `${score * 100}%` }}
          />
        </div>
      </div>

      {/* Checks grid */}
      <div className="grid grid-cols-2 gap-1.5">
        <CheckRow
          label="Покрытие"
          ok={
            report.entity_coverage_pct !== undefined &&
            report.entity_coverage_pct >= 60
          }
          value={
            report.entity_coverage_pct !== undefined
              ? `${Math.round(report.entity_coverage_pct)}%`
              : "—"
          }
        />
        <CheckRow label="Размерные цепи" ok={report.dimension_chain_ok} />
        <CheckRow label="Шероховатость Ra" ok={report.roughness_valid} />
        <CheckRow label="Допуски/посадки" ok={report.tolerance_valid} />
      </div>

      {/* Auto-fixed */}
      {report.auto_fixed && report.auto_fixed.length > 0 && (
        <div>
          <div className="text-xs text-white/40 mb-1 uppercase tracking-wider font-medium">
            Авто-исправлено ({report.auto_fixed.length})
          </div>
          <ul className="space-y-0.5">
            {report.auto_fixed.map((fix, i) => (
              <li
                key={i}
                className="text-xs text-blue-300 flex items-start gap-1.5"
              >
                <span className="text-blue-500 mt-0.5 shrink-0">✓</span>
                <span>{fix}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Warnings */}
      {report.warnings && report.warnings.length > 0 && (
        <div>
          <div className="text-xs text-white/40 mb-1 uppercase tracking-wider font-medium">
            Предупреждения ({report.warnings.length})
          </div>
          <ul className="space-y-0.5">
            {report.warnings.map((w, i) => (
              <li
                key={i}
                className="text-xs text-yellow-300 flex items-start gap-1.5"
              >
                <span className="text-yellow-500 mt-0.5 shrink-0">⚠</span>
                <span>{w}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.needs_review && (
        <div className="bg-yellow-900/20 border border-yellow-700/30 rounded px-2.5 py-2 text-xs text-yellow-300">
          Требуется ручная проверка перед использованием в техпроцессе.
        </div>
      )}
    </div>
  );
}

function CheckRow({
  label,
  ok,
  value,
}: {
  label: string;
  ok?: boolean;
  value?: string;
}) {
  return (
    <div className="flex items-center justify-between bg-zinc-800/50 rounded px-2 py-1.5">
      <span className="text-xs text-white/50 truncate">{label}</span>
      <div className="flex items-center gap-1 shrink-0">
        {value && (
          <span className="text-xs text-white/40 font-mono">{value}</span>
        )}
        {ok === true && <span className="text-green-400 text-xs">✓</span>}
        {ok === false && <span className="text-red-400 text-xs">✗</span>}
        {ok === undefined && <span className="text-white/20 text-xs">—</span>}
      </div>
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

// ── Review system ──────────────────────────────────────────────────────────────

function ReviewListPanel({
  features,
  reviewingId,
  onSelect,
}: {
  features: DrawingFeature[] | null;
  reviewingId: string | null;
  onSelect: (f: DrawingFeature) => void;
}) {
  if (features === null) {
    return (
      <div className="flex items-center justify-center h-24 text-white/30 text-xs">
        <div className="w-4 h-4 border border-white/20 border-t-white/50 rounded-full animate-spin mr-2" />
        Загрузка...
      </div>
    );
  }
  if (features.length === 0) {
    return (
      <div className="px-3 py-6 text-center">
        <div className="text-green-400 text-2xl mb-2">✓</div>
        <div className="text-xs text-white/50">
          Все элементы подтверждены.
          <br />
          Уверенность &ge; 70% по всем.
        </div>
      </div>
    );
  }
  return (
    <div className="divide-y divide-white/5">
      {features.map((f) => {
        const pct = Math.round(f.confidence * 100);
        const badgeCls =
          pct < 40 ? "bg-red-600/80 text-white" : "bg-amber-600/80 text-white";
        return (
          <button
            key={f.id}
            onClick={() => onSelect(f)}
            className={clsx(
              "w-full text-left px-3 py-2.5 hover:bg-white/5 transition-colors",
              f.id === reviewingId &&
                "bg-amber-500/10 border-l-2 border-amber-500",
            )}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-xs text-white/80 truncate">{f.name}</span>
              <span
                className={clsx(
                  "shrink-0 text-xs px-1.5 py-px rounded font-mono",
                  badgeCls,
                )}
              >
                {pct}%
              </span>
            </div>
            <div className="text-xs text-white/35 mt-0.5">{f.feature_type}</div>
          </button>
        );
      })}
    </div>
  );
}

const FEATURE_TYPE_BUTTONS: { value: string; label: string }[] = [
  { value: "hole", label: "Отверстие" },
  { value: "pocket", label: "Карман" },
  { value: "groove", label: "Канавка" },
  { value: "slot", label: "Паз" },
  { value: "thread", label: "Резьба" },
  { value: "chamfer", label: "Фаска" },
  { value: "radius", label: "Галтель" },
  { value: "surface", label: "Поверхность" },
  { value: "boss", label: "Выступ" },
  { value: "key_slot", label: "Шпоночный паз" },
  { value: "center_bore", label: "Центровое отв." },
  { value: "weld", label: "Сварной шов" },
];

function ReviewPanel({
  feature,
  remaining,
  correcting,
  onCorrect,
  onSkip,
  onClose,
}: {
  feature: DrawingFeature;
  total: number;
  remaining: number;
  correcting: boolean;
  onCorrect: (p: FeatureCorrectionPayload) => void;
  onSkip: () => void;
  onClose: () => void;
}) {
  const [selectedType, setSelectedType] = useState<string | null>(null);
  const [customType, setCustomType] = useState("");
  const [correctedName, setCorrectedName] = useState("");
  const [note, setNote] = useState("");

  const pct = Math.round(feature.confidence * 100);
  const chosenType = selectedType || customType.trim() || null;

  const handleConfirm = () => {
    if (!chosenType) return;
    onCorrect({
      original_type: feature.feature_type,
      corrected_type: chosenType,
      corrected_name: correctedName.trim() || undefined,
      note: note.trim() || undefined,
    });
    setSelectedType(null);
    setCustomType("");
    setCorrectedName("");
    setNote("");
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-amber-500/20 shrink-0">
        <span className="text-xs font-semibold text-amber-400">
          Уточните элемент
        </span>
        <div className="flex items-center gap-2">
          {remaining > 0 && (
            <span className="text-xs text-white/30">{remaining} ост.</span>
          )}
          <button
            onClick={onClose}
            className="text-white/30 hover:text-white/60 text-lg leading-none"
          >
            ×
          </button>
        </div>
      </div>

      {/* Feature info */}
      <div className="px-3 py-2.5 bg-amber-900/10 border-b border-amber-500/10 shrink-0">
        <div className="flex items-start justify-between gap-2">
          <div>
            <div className="text-xs font-medium text-white/90">
              {feature.name}
            </div>
            <div className="text-xs text-white/40 mt-0.5">
              VLM: {feature.feature_type} —{" "}
              <span className={pct < 40 ? "text-red-400" : "text-amber-400"}>
                {pct}% уверенность
              </span>
            </div>
          </div>
        </div>
        <div className="text-xs text-white/30 mt-1.5 italic">
          Контур подсвечен синим на чертеже ↑
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-3">
        {/* Type buttons */}
        <div>
          <div className="text-xs text-white/40 mb-2">Что это?</div>
          <div className="grid grid-cols-2 gap-1.5">
            {FEATURE_TYPE_BUTTONS.map((btn) => (
              <button
                key={btn.value}
                onClick={() => {
                  setSelectedType(btn.value);
                  setCustomType("");
                }}
                className={clsx(
                  "px-2 py-1.5 rounded text-xs font-medium text-left transition-colors",
                  selectedType === btn.value
                    ? "bg-amber-500 text-white"
                    : "bg-zinc-800 hover:bg-zinc-700 text-white/70",
                )}
              >
                {btn.label}
              </button>
            ))}
          </div>
        </div>

        {/* Custom type input */}
        <div>
          <label className="block text-xs text-white/40 mb-1">
            Или введите тип:
          </label>
          <input
            value={customType}
            onChange={(e) => {
              setCustomType(e.target.value);
              if (e.target.value) setSelectedType(null);
            }}
            placeholder="key_slot, counterbore..."
            className="w-full bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-xs text-white placeholder:text-white/25 focus:outline-none focus:border-amber-500/50"
          />
        </div>

        {/* Corrected name */}
        <div>
          <label className="block text-xs text-white/40 mb-1">
            Уточнённое название (необязательно):
          </label>
          <input
            value={correctedName}
            onChange={(e) => setCorrectedName(e.target.value)}
            placeholder="Шпоночный паз 12×6"
            className="w-full bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-xs text-white placeholder:text-white/25 focus:outline-none focus:border-amber-500/50"
          />
        </div>

        {/* Note */}
        <div>
          <label className="block text-xs text-white/40 mb-1">
            Примечание:
          </label>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Необязательно..."
            className="w-full bg-zinc-800 border border-white/10 rounded px-2 py-1.5 text-xs text-white placeholder:text-white/25 focus:outline-none focus:border-amber-500/50"
          />
        </div>
      </div>

      {/* Actions */}
      <div className="shrink-0 px-3 py-2.5 border-t border-white/10 flex gap-2">
        <button
          onClick={handleConfirm}
          disabled={!chosenType || correcting}
          className="flex-1 py-2 rounded bg-amber-600 hover:bg-amber-500 disabled:opacity-40 text-white text-xs font-medium transition-colors"
        >
          {correcting ? "Сохраняю..." : "Подтвердить"}
        </button>
        <button
          onClick={onSkip}
          disabled={correcting}
          className="px-3 py-2 rounded bg-zinc-800 hover:bg-zinc-700 text-white/60 text-xs transition-colors"
        >
          Пропустить
        </button>
      </div>
    </div>
  );
}
