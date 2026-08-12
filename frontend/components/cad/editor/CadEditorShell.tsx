"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import FeatureTreePanel from "@/components/cad/editor/FeatureTreePanel";
import AssumptionsStrip from "@/components/cad/editor/AssumptionsStrip";
import Viewport from "@/components/cad/editor/Viewport";
import PropertiesPanel2, {
  type AddFeatureDraftKind,
  type KernelEdgeDescriptor,
} from "@/components/cad/editor/PropertiesPanel2";
import Ribbon, {
  RibbonButton,
  RibbonDivider,
  RibbonPlaceholder,
  type RibbonTabId,
} from "@/components/cad/editor/Ribbon";
import SketchCanvas from "@/components/cad/editor/sketch/SketchCanvas";
import type { SketchProfileSegment } from "@/lib/cad-sketch-api";
import {
  buildEmgTree,
  operationBoundsFromFeatureResults,
  operationsNeedingReview,
} from "@/lib/emg-tree";
import {
  engineeringApi,
  type EngineeringModelGraphRevision,
} from "@/lib/engineering-api";
import {
  acceptVectorize,
  artifactUrl,
  getGeneration,
  rebuildSolidInput,
  solidPreviewUrl,
  type Generation,
  type Solid3dSummary,
} from "@/lib/studio-api";

const STATUS_LABEL: Record<string, string> = {
  blocked: "Заблокировано",
  built_unverified: "Собрано, не проверено",
  preview_review_required: "Черновик — требует проверки",
  verified: "Проверено",
};

function StatusBadge({ solid }: { solid?: Solid3dSummary }) {
  if (!solid) return null;
  const status =
    solid.build_status ?? (solid.built ? "built_unverified" : "blocked");
  const tone = !solid.built
    ? "bg-red-500/15 text-red-300"
    : status === "preview_review_required"
      ? "bg-amber-500/15 text-amber-300"
      : status === "verified"
        ? "bg-emerald-500/15 text-emerald-300"
        : "bg-sky-500/15 text-sky-300";
  return (
    <span className={`rounded px-2 py-1 text-[11px] font-medium ${tone}`}>
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

/** Full-screen ленточный CAD-редактор (SolidWorks/Fusion/FreeCAD-стиль:
 * вкладки-группы инструментов, дерево построения слева, вьюпорт по центру,
 * свойства/добавление фичи/эскиз справа). Заменил прежний Ф2.6c-редактор
 * (плоский тулбар, без эскиз-редактора и добавления фич) в Фазе 5 плана
 * /root/.claude/plans/starry-mapping-hippo.md — полная история: Ф0
 * (каркас) → Ф1 (лента владеет действиями) → Ф2/Ф3 (граф-native
 * добавление фич: boss/pocket/fillet/chamfer/shell/thread) → Ф4 (настоящий
 * constraint-based эскиз-редактор) → Ф5 (эта промоушен-замена). Дерево/
 * коррекция/подсказки переиспользуются как есть: FeatureTreePanel,
 * PropertiesPanel (обёрнут в PropertiesPanel2 для формы добавления фичи),
 * AssumptionsStrip, emg-tree.ts. */
export default function CadEditorShell({
  generationId,
}: {
  generationId: string;
}) {
  const [gen, setGen] = useState<Generation | null>(null);
  const [graphRevision, setGraphRevision] =
    useState<EngineeringModelGraphRevision | null>(null);
  const [selectedOperationId, setSelectedOperationId] = useState<string | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rebuildTaskId, setRebuildTaskId] = useState<string | null>(null);
  const [rebuildStatus, setRebuildStatus] = useState<string | null>(null);
  const [ribbonTab, setRibbonTab] = useState<RibbonTabId>("inspect");
  const [addFeatureDraft, setAddFeatureDraft] =
    useState<AddFeatureDraftKind | null>(null);
  // Ф4: sketching needs real screen space — active while drawing, it takes
  // over the centre viewport (in place of the 3D model) instead of being
  // squeezed into the narrow Свойства sidebar. exportedSketchProfile is the
  // handoff: PropertiesPanel2's boss/pocket form reads it once sketching
  // ends, same "lift state to the shell, hand back down" pattern
  // addFeatureDraft itself already uses.
  const [sketchModeActive, setSketchModeActive] = useState(false);
  const [exportedSketchProfile, setExportedSketchProfile] = useState<
    SketchProfileSegment[] | null
  >(null);
  // Ф6: which operation a delete request is currently in flight for — lets
  // FeatureTreePanel disable just that row's confirm button, not the whole
  // tree, while the rebuild is queued.
  const [deleteBusyId, setDeleteBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextGen, nextGraph] = await Promise.all([
        getGeneration(generationId),
        engineeringApi.getGenerationModelGraph(generationId).catch(() => null),
      ]);
      setGen(nextGen);
      setGraphRevision(nextGraph);
      setError(null);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    }
  }, [generationId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll a queued rebuild the same way EngineeringModelGraphPanel does —
  // this editor triggers the SAME task, just from more places (tree edits,
  // the toolbar's plain "Пересобрать").
  useEffect(() => {
    if (!rebuildTaskId) return;
    const timer = window.setInterval(() => {
      engineeringApi
        .getTaskStatus(rebuildTaskId)
        .then(async (task) => {
          setRebuildStatus(task.status);
          if (["SUCCESS", "FAILURE", "REVOKED"].includes(task.status)) {
            window.clearInterval(timer);
            setRebuildTaskId(null);
            // A Celery task can finish with status "SUCCESS" while its OWN
            // result payload says the build failed (cad_trace.py's tasks
            // return an {error, built: false} dict instead of raising, so
            // the pipeline's own failures stay visible in the process log
            // rather than looking like an infra crash) — checking only
            // task.status here silently reloaded stale data as if nothing
            // had gone wrong. Surface it instead.
            const resultError = (task.result as { error?: string } | null)
              ?.error;
            if (task.status === "SUCCESS" && !resultError) {
              await load();
            } else {
              setError(
                resultError ??
                  `Пересборка завершилась со статусом ${task.status}`,
              );
            }
          }
        })
        .catch((e) => {
          window.clearInterval(timer);
          setRebuildTaskId(null);
          setError(String(e));
        });
    }, 2000);
    return () => window.clearInterval(timer);
  }, [rebuildTaskId, load]);

  const tree = useMemo(
    () => (graphRevision ? buildEmgTree(graphRevision.graph) : null),
    [graphRevision],
  );

  useEffect(() => {
    if (!tree) return;
    setSelectedOperationId((current) =>
      current && tree.operations.some((op) => op.id === current)
        ? current
        : (tree.operations[0]?.id ?? null),
    );
  }, [tree]);

  const selectedOperation = useMemo(
    () => tree?.operations.find((op) => op.id === selectedOperationId) ?? null,
    [tree, selectedOperationId],
  );
  const selectedFeatures = useMemo(() => {
    if (!tree || !selectedOperation) return [];
    return selectedOperation.featureIds
      .map((fid) => tree.featuresById.get(fid.replace(/^feature:/, "")))
      .filter((f): f is NonNullable<typeof f> => Boolean(f));
  }, [tree, selectedOperation]);

  const solid = gen?.params?.solid_3d as Solid3dSummary | undefined;
  const assumptions = useMemo(() => solid?.assumptions ?? [], [solid]);
  const guessedOperationIds = useMemo(
    () => operationsNeedingReview(tree?.operations ?? [], assumptions),
    [tree, assumptions],
  );
  const operationBounds = useMemo(
    () =>
      operationBoundsFromFeatureResults(
        (solid?.verification as Record<string, unknown> | undefined)
          ?.feature_results,
      ),
    [solid],
  );
  // Ф3: edge_key candidates for the fillet/chamfer form — already exposed
  // by the kernel report (_edge_descriptors), just not yet typed on
  // Solid3dSummary; read loosely like verification.feature_results above.
  const edges = useMemo(() => {
    const raw = (solid as unknown as { kernel_report?: { edges?: unknown } })
      ?.kernel_report?.edges;
    return Array.isArray(raw) ? (raw as KernelEdgeDescriptor[]) : [];
  }, [solid]);
  const handleOperationClick = useCallback((operationId: string | null) => {
    if (operationId) setSelectedOperationId(operationId);
  }, []);

  async function handleRebuildNow() {
    setBusy(true);
    setError(null);
    try {
      const result = await rebuildSolidInput(generationId);
      if (result.rebuild_task_id) {
        setRebuildTaskId(result.rebuild_task_id);
        setRebuildStatus("QUEUED");
      } else {
        await load();
      }
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  async function handleAccept() {
    setBusy(true);
    setError(null);
    try {
      await acceptVectorize(generationId);
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  // Ф6: remove one BuildOperation. Same async-task shape as add-feature —
  // the mutation happens inside the Celery task, so this only queues it and
  // hands off to the SAME rebuildTaskId polling effect above (including its
  // own "SUCCESS but built:false" surfacing).
  async function handleDeleteOperation(operationId: string) {
    setDeleteBusyId(operationId);
    setError(null);
    try {
      const result = await engineeringApi.removeGenerationModelGraphFeature(
        generationId,
        operationId,
        {
          note: "Удалено из редактора",
          idempotency_key: `remove-feature:${crypto.randomUUID()}`,
        },
      );
      setRebuildTaskId(result.rebuild_task_id);
      setRebuildStatus("QUEUED");
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setDeleteBusyId(null);
    }
  }

  if (!gen) {
    return (
      <div className="grid h-screen place-items-center bg-zinc-950 text-sm text-zinc-500">
        {error ?? "Загрузка…"}
      </div>
    );
  }

  const previewKind =
    solid?.build_status === "preview_review_required" ? "preview" : "artifact";
  const modelUrl =
    previewKind === "preview"
      ? solidPreviewUrl(generationId, "stl")
      : artifactUrl(generationId, "stl");
  const topologyUrl =
    previewKind === "preview" && solid?.paths?.topology
      ? solidPreviewUrl(generationId, "topology")
      : gen.accepted
        ? artifactUrl(generationId, "topology")
        : undefined;
  const hasModel =
    previewKind === "preview" ? Boolean(solid?.paths?.stl) : gen.accepted;

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-zinc-950 text-zinc-100">
      <header className="flex flex-wrap items-center gap-3 border-b border-white/10 bg-zinc-900 px-3 py-2">
        <Link
          href={`/cad/${generationId}`}
          className="rounded border border-white/15 px-2.5 py-1.5 text-xs text-zinc-300 hover:bg-white/5"
        >
          ← Обычный вид
        </Link>
        <h1 className="min-w-0 flex-1 truncate text-sm font-medium text-zinc-100">
          {gen.prompt ||
            (gen.params?.source_filename as string | undefined) ||
            "CAD-редактор"}
        </h1>
        <StatusBadge solid={solid} />
        {gen.accepted && (
          <span className="rounded bg-emerald-500/15 px-2 py-1 text-[11px] font-medium text-emerald-300">
            Принято
          </span>
        )}
        {rebuildTaskId && (
          <span className="text-[11px] text-sky-300">
            Пересборка… {rebuildStatus}
          </span>
        )}
      </header>

      <Ribbon active={ribbonTab} onChange={setRibbonTab}>
        {ribbonTab === "sketch" && (
          <span className="px-2 text-[11px] text-zinc-500">
            {sketchModeActive
              ? "Эскиз открыт в центральной области — рисуйте контур, затем «Использовать этот контур»."
              : "Чтобы начать эскиз: вкладка Фичи → Бобышка/Карман → профиль «Эскиз»."}
          </span>
        )}
        {ribbonTab === "features" && (
          <>
            <RibbonButton
              icon="⬆"
              label="Бобышка"
              onClick={() => setAddFeatureDraft("boss")}
              disabled={!hasModel}
              title={
                hasModel ? undefined : "Сначала нужна построенная 3D-модель"
              }
            />
            <RibbonButton
              icon="⬇"
              label="Карман"
              onClick={() => setAddFeatureDraft("pocket")}
              disabled={!hasModel}
              title={
                hasModel ? undefined : "Сначала нужна построенная 3D-модель"
              }
            />
            <RibbonDivider />
            <RibbonButton
              icon="⌒"
              label="Скругление"
              onClick={() => setAddFeatureDraft("fillet")}
              disabled={!hasModel}
              title={
                hasModel ? undefined : "Сначала нужна построенная 3D-модель"
              }
            />
            <RibbonButton
              icon="⟂"
              label="Фаска"
              onClick={() => setAddFeatureDraft("chamfer")}
              disabled={!hasModel}
              title={
                hasModel ? undefined : "Сначала нужна построенная 3D-модель"
              }
            />
            <RibbonButton
              icon="▢"
              label="Оболочка"
              onClick={() => setAddFeatureDraft("shell")}
              disabled={!hasModel}
              title={
                hasModel ? undefined : "Сначала нужна построенная 3D-модель"
              }
            />
            <RibbonButton
              icon="⚙"
              label="Резьба"
              onClick={() => setAddFeatureDraft("thread")}
              disabled={!hasModel}
              title={
                hasModel ? undefined : "Сначала нужна построенная 3D-модель"
              }
            />
          </>
        )}
        {ribbonTab === "body" && (
          <>
            <RibbonPlaceholder
              icon="∪"
              label="Объединение"
              comingIn="будущей фазе"
            />
            <RibbonPlaceholder
              icon="∩"
              label="Пересечение"
              comingIn="будущей фазе"
            />
            <RibbonPlaceholder
              icon="▦"
              label="Массив"
              comingIn="будущей фазе"
            />
          </>
        )}
        {ribbonTab === "inspect" && (
          <>
            <RibbonButton
              icon="↻"
              label="Пересобрать"
              onClick={() => void handleRebuildNow()}
              disabled={busy || Boolean(rebuildTaskId)}
            />
            {!gen.accepted && (
              <RibbonButton
                icon="✓"
                label="Принять"
                onClick={() => void handleAccept()}
                disabled={busy}
              />
            )}
            <RibbonDivider />
            <ExportMenu generationId={generationId} accepted={gen.accepted} />
          </>
        )}
      </Ribbon>

      {error && (
        <div className="border-b border-red-500/30 bg-red-500/10 px-3 py-1.5 text-xs text-red-300">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <aside className="w-64 shrink-0 overflow-y-auto border-r border-white/10 bg-zinc-900/60">
          <div className="border-b border-white/10 px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            Дерево построения
          </div>
          {tree ? (
            <FeatureTreePanel
              operations={tree.operations}
              guessedOperationIds={guessedOperationIds}
              selectedId={selectedOperationId}
              onSelect={setSelectedOperationId}
              onDelete={(id) => void handleDeleteOperation(id)}
              deleteBusyId={deleteBusyId}
            />
          ) : (
            <p className="p-3 text-xs text-zinc-500">
              Граф ещё не построен для этой генерации.
            </p>
          )}
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          {sketchModeActive ? (
            <SketchCanvas
              onCancel={() => setSketchModeActive(false)}
              onExported={(profile) => {
                setExportedSketchProfile(profile);
                setSketchModeActive(false);
              }}
              onError={setError}
            />
          ) : (
            <>
              <Viewport
                hasModel={hasModel}
                modelUrl={modelUrl}
                topologyUrl={topologyUrl}
                operationBounds={operationBounds}
                flaggedOperationIds={guessedOperationIds}
                selectedOperationId={selectedOperationId}
                onOperationClick={handleOperationClick}
              />
              <AssumptionsStrip
                assumptions={assumptions}
                operations={tree?.operations ?? []}
                onSelectOperation={setSelectedOperationId}
              />
            </>
          )}
        </main>

        <aside className="w-96 shrink-0 overflow-y-auto border-l border-white/10 bg-zinc-900/60">
          <div className="border-b border-white/10 px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            Свойства
          </div>
          <PropertiesPanel2
            generationId={generationId}
            operation={selectedOperation}
            features={selectedFeatures}
            edges={edges}
            addFeatureDraft={addFeatureDraft}
            onAddFeatureDraftChange={setAddFeatureDraft}
            sketchModeActive={sketchModeActive}
            onStartSketch={() => setSketchModeActive(true)}
            exportedSketchProfile={exportedSketchProfile}
            onSketchProfileConsumed={() => setExportedSketchProfile(null)}
            onSaved={() => void load()}
            onRebuildQueued={(taskId) => {
              setRebuildTaskId(taskId);
              setRebuildStatus("QUEUED");
            }}
            onError={setError}
          />
        </aside>
      </div>
    </div>
  );
}

function ExportMenu({
  generationId,
  accepted,
}: {
  generationId: string;
  accepted: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!accepted) {
    return (
      <span
        title="Экспорт доступен после приёмки"
        className="rounded border border-white/10 px-3 py-1.5 text-xs text-zinc-600"
      >
        ⭳ Экспорт
      </span>
    );
  }
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="rounded border border-white/15 px-3 py-1.5 text-xs text-zinc-200 hover:bg-white/5"
      >
        ⭳ Экспорт ▾
      </button>
      {open && (
        <div
          className="absolute right-0 z-10 mt-1 w-40 rounded border border-white/10 bg-zinc-900 py-1 shadow-lg"
          onMouseLeave={() => setOpen(false)}
        >
          {(["dxf", "step", "iges", "stl", "pdf"] as const).map((kind) => (
            <a
              key={kind}
              href={artifactUrl(generationId, kind)}
              download
              className="block px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/10"
            >
              {kind.toUpperCase()}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
