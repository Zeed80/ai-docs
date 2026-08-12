"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import CadModelViewer from "@/components/studio/CadModelViewer";
import FeatureTreePanel from "@/components/cad/editor/FeatureTreePanel";
import PropertiesPanel from "@/components/cad/editor/PropertiesPanel";
import AssumptionsStrip from "@/components/cad/editor/AssumptionsStrip";
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

/** Ф2.6c/B2-B3: a fully-featured CAD editor for one digitization result —
 * a single full-screen surface instead of a small viewport buried in a
 * long page. Owns the graph, the selection and the 3D viewport together so
 * a click in any panel is immediately reflected in the others.
 *
 * Superseded by components/cad/editor2/CadEditorShell2.tsx (see
 * /root/.claude/plans/starry-mapping-hippo.md) — this file stays live at
 * /cad/[id]/editor only until editor2 reaches parity and is promoted in
 * that plan's Фаза 5, then gets deleted. The old Ф10 2D-IR add-feature
 * flow (Cad3dPanel.tsx) it used to sit alongside has already been removed
 * (Фаза 3): editor2's add-feature endpoint is graph-native and covers
 * everything Cad3dPanel did, plus more kinds. */
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
            // A Celery task can finish "SUCCESS" while its OWN result
            // payload says the build failed (cad_trace.py returns
            // {error, built: false} instead of raising) — checking only
            // task.status silently reloaded stale data as if nothing had
            // gone wrong. Surface it instead.
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
  // Driven by solid_3d.assumptions (A2's own curated notes), NOT raw
  // Assertion.origin — see operationsNeedingReview's own docstring for why
  // the naive per-param check lit up nearly the whole tree amber live.
  const guessedOperationIds = useMemo(
    () => operationsNeedingReview(tree?.operations ?? [], assumptions),
    [tree, assumptions],
  );
  // B3: solid_3d.verification.feature_results already carries the kernel's
  // own measured B-Rep-delta bounds per operation — no extra fetch, no new
  // backend endpoint, just re-reading data already on `gen`.
  const operationBounds = useMemo(
    () =>
      operationBoundsFromFeatureResults(
        (solid?.verification as Record<string, unknown> | undefined)
          ?.feature_results,
      ),
    [solid],
  );
  // B2: a click on the model resolves (via CadModelViewer's own bounds
  // containment test) to the operation id whose measured box contains the
  // hit point. Ambiguous/empty clicks (id === null) leave the current
  // selection alone rather than guessing.
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
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy || Boolean(rebuildTaskId)}
            onClick={() => void handleRebuildNow()}
            className="rounded border border-white/15 px-3 py-1.5 text-xs text-zinc-200 hover:bg-white/5 disabled:opacity-40"
          >
            ↻ Пересобрать
          </button>
          {!gen.accepted && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void handleAccept()}
              className="rounded bg-emerald-500/20 px-3 py-1.5 text-xs text-emerald-200 hover:bg-emerald-500/30 disabled:opacity-40"
            >
              ✓ Принять
            </button>
          )}
          <ExportMenu generationId={generationId} accepted={gen.accepted} />
        </div>
      </header>

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
            />
          ) : (
            <p className="p-3 text-xs text-zinc-500">
              Граф ещё не построен для этой генерации.
            </p>
          )}
        </aside>

        <main className="flex min-w-0 flex-1 flex-col">
          <div className="relative min-h-0 flex-1">
            {hasModel && guessedOperationIds.size > 0 && (
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
                flaggedOperationIds={guessedOperationIds}
                selectedOperationId={selectedOperationId}
                onOperationClick={handleOperationClick}
              />
            ) : (
              <div className="grid h-full place-items-center text-sm text-zinc-600">
                3D-модель ещё не построена
              </div>
            )}
          </div>
          <AssumptionsStrip
            assumptions={assumptions}
            operations={tree?.operations ?? []}
            onSelectOperation={setSelectedOperationId}
          />
        </main>

        <aside className="w-96 shrink-0 overflow-y-auto border-l border-white/10 bg-zinc-900/60">
          <div className="border-b border-white/10 px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-zinc-500">
            Свойства
          </div>
          <PropertiesPanel
            generationId={generationId}
            operation={selectedOperation}
            features={selectedFeatures}
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
