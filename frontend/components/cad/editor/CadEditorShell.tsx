"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import {
  ArrowDownCircle,
  ArrowUpCircle,
  Check,
  Download,
  ExternalLink,
  Grid3x3,
  Maximize,
  Minimize,
  RotateCw,
  Redo2,
  Settings,
  Slash,
  Spline,
  Square,
  Undo2,
  SquaresIntersect,
  SquaresUnite,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import FeatureTreePanel, {
  type OperationTreeIssue,
} from "@/components/cad/editor/FeatureTreePanel";
import AssumptionsStrip, {
  type EdgeRepairTarget,
} from "@/components/cad/editor/AssumptionsStrip";
import BlockersAlternativesPanel from "@/components/cad/editor/BlockersAlternativesPanel";
import CadStatusBar from "@/components/cad/editor/CadStatusBar";
import ResizablePane from "@/components/cad/editor/ResizablePane";
import Viewport from "@/components/cad/editor/Viewport";
import PropertiesPanel2, {
  type AddFeatureDraftKind,
} from "@/components/cad/editor/PropertiesPanel2";
import OperationProvenancePanel from "@/components/cad/editor/OperationProvenancePanel";
import TopologyPickerPanel from "@/components/cad/editor/TopologyPickerPanel";
import Ribbon, {
  RibbonButton,
  RibbonDivider,
  RibbonGroup,
  RibbonPlaceholder,
  type RibbonTabId,
} from "@/components/cad/editor/Ribbon";
import SketchCanvas from "@/components/cad/editor/sketch/SketchCanvas";
import SpecEditorPanel from "@/components/cad/SpecEditorPanel";
import type { SketchProfileSegment } from "@/lib/cad-sketch-api";
import {
  buildEmgTree,
  operationBoundsFromFeatureResults,
  operationsNeedingReview,
} from "@/lib/emg-tree";
import {
  engineeringApi,
  type EngineeringDesignHistory,
  type EngineeringModelGraphRevision,
} from "@/lib/engineering-api";
import {
  approveCadAsDrafter,
  approveCadAsNormcontroller,
  artifactUrl,
  correctSpec,
  diagnosticsPackageUrl,
  getCadCertification,
  getGeneration,
  rebuildSolidInput,
  solidPreviewUrl,
  type CadCertification,
  type Generation,
  type KernelEdgeDescriptor,
  type KernelFaceDescriptor,
  type Solid3dSummary,
  type SpecAssumption,
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

/** Ф10: toggles the browser's own Fullscreen API on the whole shell — the
 * route is already chromeless (no app sidebar/chat, see client-layout.tsx)
 * and already opens in its own tab (target="_blank" on the "Открыть в
 * редакторе" link in /cad/[id]/page.tsx); this is the one further step
 * available without introducing a real desktop shell (Electron/Tauri —
 * deliberately not pursued, see the plan's own note): hiding the
 * browser's OWN chrome (address bar etc) too, closer to "как обычное
 * приложение". */
function FullscreenButton({
  targetRef,
}: {
  targetRef: React.RefObject<HTMLDivElement | null>;
}) {
  const [isFullscreen, setIsFullscreen] = useState(false);
  useEffect(() => {
    const onChange = () => setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);
  return (
    <button
      type="button"
      onClick={() => {
        if (document.fullscreenElement) {
          void document.exitFullscreen();
        } else {
          void targetRef.current?.requestFullscreen();
        }
      }}
      title={isFullscreen ? "Выйти из полноэкранного режима" : "На весь экран"}
      className="rounded border border-white/15 p-1.5 text-zinc-300 hover:bg-white/5"
    >
      {isFullscreen ? (
        <Minimize className="h-4 w-4" />
      ) : (
        <Maximize className="h-4 w-4" />
      )}
    </button>
  );
}

/** Full-screen ленточный CAD-редактор (SolidWorks/Fusion/FreeCAD-стиль:
 * вкладки-группы инструментов, дерево построения слева, вьюпорт по центру,
 * свойства/добавление фичи/эскиз справа). Заменил прежний Ф2.6c-редактор
 * (плоский тулбар, без эскиз-редактора и добавления фич) в Фазе 5 плана
 * /root/.claude/plans/starry-mapping-hippo.md — полная история: Ф0
 * (каркас) → Ф1 (лента владеет действиями) → Ф2/Ф3 (граф-native
 * добавление фич: boss/pocket/fillet/chamfer/shell/thread) → Ф4 (настоящий
 * constraint-based эскиз-редактор) → Ф5 (эта промоушен-замена) → Ф6-Ф9
 * (удаление операций, клик-выбор ребра, массивы, дуга) → Ф10 (эта
 * UX-полировка: иконки, resizable-панели, fullscreen, статус-бар, горячие
 * клавиши). Дерево/коррекция/подсказки переиспользуются как есть:
 * FeatureTreePanel, PropertiesPanel (обёрнут в PropertiesPanel2 для формы
 * добавления фичи), AssumptionsStrip, emg-tree.ts. */
export default function CadEditorShell({
  generationId,
}: {
  generationId: string;
}) {
  // B: SpecEditorPanel's own strings all live under this namespace already
  // (CadWorkspace.tsx uses the same one) — the ribbon shell otherwise never
  // needed next-intl at all (every other string here is a hardcoded literal).
  const t = useTranslations("studio");
  const shellRef = useRef<HTMLDivElement | null>(null);
  const [gen, setGen] = useState<Generation | null>(null);
  const [graphRevision, setGraphRevision] =
    useState<EngineeringModelGraphRevision | null>(null);
  const [selectedOperationId, setSelectedOperationId] = useState<string | null>(
    null,
  );
  const [focusedAssertionId, setFocusedAssertionId] = useState<string | null>(
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
  // Ф7: same one-shot handoff pattern as exportedSketchProfile — a click on
  // the 3D model hands its edge_key down to whichever fillet/chamfer form
  // is currently open.
  const [pickedEdgeKey, setPickedEdgeKey] = useState<string | null>(null);
  const [selectedFaceKey, setSelectedFaceKey] = useState<string | null>(null);
  const [selectedEdgeKey, setSelectedEdgeKey] = useState<string | null>(null);
  // C: which chamfers[i]/fillets[i] row a click on the model should repair —
  // set from AssumptionsStrip's "Указать ребро на модели" button on an
  // unplaceable-edge note (no BuildOperation exists for it to select
  // instead, see _edge_features/_edge_selector in cad_solid.py).
  const [edgeRepairTarget, setEdgeRepairTarget] =
    useState<EdgeRepairTarget | null>(null);
  // D: one-shot handoff, same shape as pickedEdgeKey — a click on a face
  // while a boss/pocket draft is open hands its centroid down to that
  // form's centerX/centerY (KernelFaceDescriptor.center_of_mass_mm is
  // already computed server-side; face clicking itself was already wired
  // for inspection, just never fed anything actionable — see Viewport's
  // own onFaceSelect).
  const [pickedFaceCenter, setPickedFaceCenter] = useState<{
    x: number;
    y: number;
  } | null>(null);
  // Ф8: true while the "Массив" form is open — operates on selectedOperationId,
  // not a new draft.
  const [patternDraftActive, setPatternDraftActive] = useState(false);
  const [certification, setCertification] = useState<CadCertification | null>(
    null,
  );
  const [designHistory, setDesignHistory] =
    useState<EngineeringDesignHistory | null>(null);
  const [historyCursor, setHistoryCursor] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const [nextGen, nextGraph, nextCertification, nextHistory] =
        await Promise.all([
          getGeneration(generationId),
          engineeringApi
            .getGenerationModelGraph(generationId)
            .catch(() => null),
          getCadCertification(generationId).catch(() => null),
          engineeringApi
            .getGenerationDesignHistory(generationId)
            .catch(() => null),
        ]);
      setGen(nextGen);
      setGraphRevision(nextGraph);
      setCertification(nextCertification);
      setDesignHistory(nextHistory);
      setHistoryCursor((current) =>
        current !== null &&
        nextHistory?.revisions.some((item) => item.revision === current)
          ? current
          : (nextHistory?.current_revision ?? null),
      );
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
  // B/C: declared here (not next to hasModel below, past the early "if
  // (!gen) return" guard) because handleRepairEdge — defined further up
  // than that guard — needs it in its useCallback dependency array, which
  // is evaluated synchronously on every render; gen?. keeps this safe even
  // on the render where gen is still null.
  const specForEditing = (gen?.params?.spec_corrected ?? gen?.params?.spec) as
    Record<string, unknown> | undefined;
  const specAssumptions = (gen?.params?.spec_assumptions ??
    []) as SpecAssumption[];
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
  const edges = useMemo<KernelEdgeDescriptor[]>(
    () => solid?.kernel_report?.edges ?? [],
    [solid],
  );
  const faces = useMemo<KernelFaceDescriptor[]>(
    () => solid?.kernel_report?.faces ?? [],
    [solid],
  );
  const operationIssues = useMemo(() => {
    const issues = new Map<string, OperationTreeIssue[]>();
    const add = (operationId: string, issue: OperationTreeIssue) => {
      const current = issues.get(operationId) ?? [];
      if (
        !current.some(
          (item) => item.code === issue.code && item.message === issue.message,
        )
      ) {
        current.push(issue);
        issues.set(operationId, current);
      }
    };
    const operationByFeature = new Map<string, string>();
    for (const operation of tree?.operations ?? []) {
      for (const featureId of operation.featureIds) {
        operationByFeature.set(featureId, operation.id);
      }
    }
    for (const item of solid?.kernel_report?.feature_results ?? []) {
      const index = Number(item.feature_index);
      if (!Number.isInteger(index) || String(item.status) !== "failed")
        continue;
      const operationId = `operation:${index}`;
      if (!tree?.operations.some((operation) => operation.id === operationId))
        continue;
      add(operationId, {
        code: "KERNEL_FEATURE_FAILED",
        message: String(item.reason ?? "Kernel operation failed"),
        source: "kernel",
      });
    }
    const criticalIds = new Set(
      graphRevision?.graph.verification?.critical_unresolved_assertion_ids ??
        [],
    );
    for (const assertion of graphRevision?.graph.assertions ?? []) {
      if (!criticalIds.has(assertion.id) || assertion.state !== "active")
        continue;
      const operationId = assertion.subject_id.startsWith("operation:")
        ? assertion.subject_id
        : operationByFeature.get(assertion.subject_id);
      if (!operationId) continue;
      add(operationId, {
        code: "CRITICAL_ASSERTION_UNRESOLVED",
        message: assertion.predicate,
        source: "model_graph",
      });
    }
    return issues;
  }, [graphRevision, solid, tree]);
  const handleOperationClick = useCallback((operationId: string | null) => {
    if (operationId) {
      setFocusedAssertionId(null);
      setSelectedOperationId(operationId);
    }
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

  async function handleCertification() {
    setBusy(true);
    setError(null);
    try {
      const next =
        certification?.status === "drafter_approved"
          ? await approveCadAsNormcontroller(generationId)
          : await approveCadAsDrafter(generationId, "mechanical");
      setCertification(next);
      await load();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  }

  const restoreHistory = useCallback(
    async (direction: "undo" | "redo") => {
      const revisions = designHistory?.revisions ?? [];
      if (revisions.length < 2 || historyCursor === null) return;
      const currentIndex = revisions.findIndex(
        (item) => item.revision === historyCursor,
      );
      const targetIndex = currentIndex + (direction === "undo" ? -1 : 1);
      const target = revisions[targetIndex];
      if (!target) return;
      const reason = window.prompt(
        direction === "undo"
          ? `Причина отката к ревизии r${target.revision}:`
          : `Причина возврата к ревизии r${target.revision}:`,
      );
      if (!reason?.trim()) return;
      setBusy(true);
      setError(null);
      try {
        const result = await engineeringApi.restoreGenerationDesignRevision(
          generationId,
          {
            target_revision: target.revision,
            note: reason.trim(),
            idempotency_key: `restore-design:${crypto.randomUUID()}`,
          },
        );
        setHistoryCursor(target.revision);
        setRebuildTaskId(result.rebuild_task_id);
        setRebuildStatus("QUEUED");
      } catch (e) {
        setError(String((e as Error).message ?? e));
      } finally {
        setBusy(false);
      }
    },
    [designHistory, generationId, historyCursor],
  );

  // Ф6: remove one BuildOperation. Same async-task shape as add-feature —
  // the mutation happens inside the Celery task, so this only queues it and
  // hands off to the SAME rebuildTaskId polling effect above (including its
  // own "SUCCESS but built:false" surfacing).
  const handleDeleteOperation = useCallback(
    async (operationId: string, reason: string) => {
      setDeleteBusyId(operationId);
      setError(null);
      try {
        const result = await engineeringApi.removeGenerationModelGraphFeature(
          generationId,
          operationId,
          {
            note: reason,
            idempotency_key: `remove-feature:${crypto.randomUUID()}`,
          },
        );
        setHistoryCursor(null);
        setRebuildTaskId(result.rebuild_task_id);
        setRebuildStatus("QUEUED");
      } catch (e) {
        setError(String((e as Error).message ?? e));
      } finally {
        setDeleteBusyId(null);
      }
    },
    [generationId],
  );

  // C: a human clicked the real edge on the model to resolve a chamfer/
  // fillet the reader named a place for ("shoulder", "bore") but the kernel
  // couldn't find (_edge_selector returned None, cad_solid.py) — there is no
  // BuildOperation for it, only the raw spec array the compiler reads
  // directly, so this patches spec.main_view.chamfers[i]/fillets[i] and
  // rebuilds, the same call SpecEditorPanel's own edit form makes.
  const handleRepairEdge = useCallback(
    async (target: EdgeRepairTarget, edgeKey: string) => {
      const edge = edges.find((item) => item.key === edgeKey);
      if (!edge || edge.vertices.length === 0) {
        setError("Не удалось получить геометрию выбранного ребра");
        return;
      }
      const atZ =
        edge.vertices.reduce((sum, v) => sum + v.z, 0) / edge.vertices.length;
      const atDiameter =
        (edge.vertices.reduce((sum, v) => sum + Math.hypot(v.x, v.y), 0) /
          edge.vertices.length) *
        2;
      const listKey = target.kind === "chamfer" ? "chamfers" : "fillets";
      const body = (specForEditing?.main_view ?? {}) as Record<string, unknown>;
      const list = ((body[listKey] ?? []) as Record<string, unknown>[]).map(
        (item) => ({ ...item }),
      );
      if (target.index >= list.length) {
        setError(
          `Не найдена запись ${listKey}[${target.index}] в спецификации — возможно, она уже была изменена`,
        );
        return;
      }
      list[target.index] = {
        ...list[target.index],
        at_z_mm: Math.round(atZ * 100) / 100,
        at_diameter_mm: Math.round(atDiameter * 100) / 100,
      };
      setBusy(true);
      setError(null);
      try {
        const result = await correctSpec(
          generationId,
          { [listKey]: list },
          { rebuild: true },
        );
        setEdgeRepairTarget(null);
        setSelectedEdgeKey(null);
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
    },
    [edges, generationId, load, specForEditing],
  );

  // Ф10: Delete (with a plain window.confirm — the tree row's own inline
  // Да/Отмена needs a hover state a keyboard press doesn't have) removes
  // the SELECTED operation; Escape backs out of whichever draft/mode is
  // currently open, closest-scoped first (sketch, then add-feature/
  // pattern drafts) — never closes the whole editor, only the in-progress
  // action, matching every Отмена button already in these panels.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable);
      if (typing) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        void restoreHistory(event.shiftKey ? "redo" : "undo");
        return;
      }
      if (event.key === "Delete" && selectedOperationId && !deleteBusyId) {
        const label = selectedOperation
          ? selectedOperation.kind
          : "выбранную операцию";
        if (window.confirm(`Удалить операцию «${label}»?`)) {
          const reason = window.prompt("Укажите причину удаления операции:");
          if (reason?.trim()) {
            void handleDeleteOperation(selectedOperationId, reason.trim());
          }
        }
        return;
      }
      if (event.key === "Escape") {
        if (sketchModeActive) {
          setSketchModeActive(false);
        } else if (edgeRepairTarget) {
          setEdgeRepairTarget(null);
        } else if (addFeatureDraft) {
          setAddFeatureDraft(null);
        } else if (patternDraftActive) {
          setPatternDraftActive(false);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    selectedOperationId,
    selectedOperation,
    deleteBusyId,
    sketchModeActive,
    edgeRepairTarget,
    addFeatureDraft,
    patternDraftActive,
    handleDeleteOperation,
    restoreHistory,
  ]);

  if (!gen) {
    return (
      <div className="grid h-screen place-items-center bg-zinc-950 text-sm text-zinc-400">
        {error ?? "Загрузка…"}
      </div>
    );
  }

  const previewKind = !gen.accepted ? "preview" : "artifact";
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
  const hasModel = Boolean(solid?.built && solid?.paths?.stl);

  return (
    <div
      ref={shellRef}
      className="flex h-screen flex-col overflow-hidden bg-zinc-950 text-zinc-100"
    >
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
        <span
          className={`rounded px-2 py-1 text-[11px] font-medium ${
            graphRevision?.graph_role === "design"
              ? "bg-violet-500/15 text-violet-300"
              : "bg-zinc-500/15 text-zinc-300"
          }`}
          title="Правки конструкции хранятся отдельно от результата распознавания"
        >
          {graphRevision?.graph_role === "design"
            ? "Ветка конструкции"
            : "Оцифровка — исходная ветка"}
        </span>
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
        <FullscreenButton targetRef={shellRef} />
      </header>

      <Ribbon active={ribbonTab} onChange={setRibbonTab}>
        {ribbonTab === "sketch" && (
          <span className="px-2 text-[11px] text-zinc-400">
            {sketchModeActive
              ? "Эскиз открыт в центральной области — рисуйте контур, затем «Использовать этот контур»."
              : "Чтобы начать эскиз: вкладка Фичи → Бобышка/Карман → профиль «Эскиз»."}
          </span>
        )}
        {ribbonTab === "features" && (
          <>
            <RibbonGroup label="Профиль">
              <RibbonButton
                icon={<ArrowUpCircle className="h-4 w-4" />}
                label="Бобышка"
                onClick={() => setAddFeatureDraft("boss")}
                disabled={!hasModel}
                title={
                  hasModel ? undefined : "Сначала нужна построенная 3D-модель"
                }
              />
              <RibbonButton
                icon={<ArrowDownCircle className="h-4 w-4" />}
                label="Карман"
                onClick={() => setAddFeatureDraft("pocket")}
                disabled={!hasModel}
                title={
                  hasModel ? undefined : "Сначала нужна построенная 3D-модель"
                }
              />
            </RibbonGroup>
            <RibbonDivider />
            <RibbonGroup label="Модификация">
              <RibbonButton
                icon={<Spline className="h-4 w-4" />}
                label="Скругление"
                onClick={() => setAddFeatureDraft("fillet")}
                disabled={!hasModel}
                title={
                  hasModel ? undefined : "Сначала нужна построенная 3D-модель"
                }
              />
              <RibbonButton
                icon={<Slash className="h-4 w-4" />}
                label="Фаска"
                onClick={() => setAddFeatureDraft("chamfer")}
                disabled={!hasModel}
                title={
                  hasModel ? undefined : "Сначала нужна построенная 3D-модель"
                }
              />
              <RibbonButton
                icon={<Square className="h-4 w-4" />}
                label="Оболочка"
                onClick={() => setAddFeatureDraft("shell")}
                disabled={!hasModel}
                title={
                  hasModel ? undefined : "Сначала нужна построенная 3D-модель"
                }
              />
              <RibbonButton
                icon={<Settings className="h-4 w-4" />}
                label="Резьба"
                onClick={() => setAddFeatureDraft("thread")}
                disabled={!hasModel}
                title={
                  hasModel ? undefined : "Сначала нужна построенная 3D-модель"
                }
              />
            </RibbonGroup>
          </>
        )}
        {ribbonTab === "body" && (
          <RibbonGroup label="Тело">
            <RibbonPlaceholder
              icon={<SquaresUnite className="h-4 w-4" />}
              label="Объединение"
              comingIn="отдельном плане (нет прецедента в ядре)"
            />
            <RibbonPlaceholder
              icon={<SquaresIntersect className="h-4 w-4" />}
              label="Пересечение"
              comingIn="отдельном плане (нет прецедента в ядре)"
            />
            <RibbonButton
              icon={<Grid3x3 className="h-4 w-4" />}
              label="Массив"
              onClick={() => setPatternDraftActive(true)}
              disabled={!hasModel || !selectedOperationId}
              title={
                !hasModel
                  ? "Сначала нужна построенная 3D-модель"
                  : !selectedOperationId
                    ? "Сначала выберите операцию в дереве"
                    : undefined
              }
            />
          </RibbonGroup>
        )}
        {ribbonTab === "spec" && (
          <span className="px-2 text-[11px] text-zinc-400">
            Наружный/внутренний профиль, фаски, скругления, резьбовые отверстия
            — правки ниже, в центральной области. То же исправление, что раньше
            было только на обычной странице чертежа.
          </span>
        )}
        {ribbonTab === "inspect" && (
          <RibbonGroup label="Сборка">
            <RibbonButton
              icon={<Undo2 className="h-4 w-4" />}
              label="Отменить"
              onClick={() => void restoreHistory("undo")}
              disabled={
                busy ||
                Boolean(rebuildTaskId) ||
                historyCursor === null ||
                !designHistory?.revisions.some(
                  (item) => item.revision < historyCursor,
                )
              }
              title="Создать новую ревизию с содержимым предыдущей (Ctrl+Z)"
            />
            <RibbonButton
              icon={<Redo2 className="h-4 w-4" />}
              label="Повторить"
              onClick={() => void restoreHistory("redo")}
              disabled={
                busy ||
                Boolean(rebuildTaskId) ||
                historyCursor === null ||
                !designHistory?.revisions.some(
                  (item) => item.revision > historyCursor,
                )
              }
              title="Вернуть отменённое состояние (Ctrl+Shift+Z)"
            />
            <RibbonDivider />
            <RibbonButton
              icon={<RotateCw className="h-4 w-4" />}
              label="Пересобрать"
              onClick={() => void handleRebuildNow()}
              disabled={busy || Boolean(rebuildTaskId)}
            />
            {!gen.accepted && (
              <RibbonButton
                icon={<Check className="h-4 w-4" />}
                label={
                  certification?.status === "drafter_approved"
                    ? "Нормоконтроль"
                    : "Подписать"
                }
                onClick={() => void handleCertification()}
                disabled={busy}
                title={
                  certification?.status === "drafter_approved"
                    ? "Финальная подпись нормоконтролёра другим пользователем"
                    : "Подпись чертёжника для текущей неизменяемой ревизии"
                }
              />
            )}
            <RibbonDivider />
            <ExportMenu generationId={generationId} accepted={gen.accepted} />
          </RibbonGroup>
        )}
      </Ribbon>

      {error && (
        <div className="border-b border-red-500/30 bg-red-500/10 px-3 py-1.5 text-xs text-red-300">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <ResizablePane
          storageKey="cad-editor:treeWidth"
          defaultWidth={256}
          minWidth={180}
          maxWidth={420}
          side="left"
        >
          <aside className="h-full overflow-y-auto border-r border-white/10 bg-zinc-900/60">
            <div className="border-b border-white/10 px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
              Дерево построения
            </div>
            {tree ? (
              <FeatureTreePanel
                operations={tree.operations}
                guessedOperationIds={guessedOperationIds}
                selectedId={selectedOperationId}
                onSelect={(id) => {
                  setFocusedAssertionId(null);
                  setSelectedOperationId(id);
                }}
                onDelete={(id, reason) =>
                  void handleDeleteOperation(id, reason)
                }
                deleteBusyId={deleteBusyId}
                operationIssues={operationIssues}
              />
            ) : (
              <p className="p-3 text-xs text-zinc-400">
                Граф ещё не построен для этой генерации.
              </p>
            )}
          </aside>
        </ResizablePane>

        <main className="flex min-w-0 flex-1 flex-col">
          {ribbonTab === "spec" ? (
            <div className="min-h-0 flex-1 overflow-y-auto p-3">
              <SpecEditorPanel
                generationId={generationId}
                spec={specForEditing}
                assumptions={specAssumptions}
                busy={busy}
                onDone={(result) => {
                  if (result?.rebuild_task_id) {
                    setRebuildTaskId(result.rebuild_task_id);
                    setRebuildStatus("QUEUED");
                  } else {
                    void load();
                  }
                }}
                onError={setError}
                t={t}
              />
            </div>
          ) : sketchModeActive ? (
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
                selectedFaceKey={selectedFaceKey}
                onFaceSelect={(key) => {
                  setSelectedFaceKey(key);
                  if (key) setSelectedEdgeKey(null);
                  if (
                    key &&
                    (addFeatureDraft === "boss" || addFeatureDraft === "pocket")
                  ) {
                    const face = faces.find((item) => item.key === key);
                    if (face) {
                      setPickedFaceCenter({
                        x: face.center_of_mass_mm.x,
                        y: face.center_of_mass_mm.y,
                      });
                    }
                  }
                }}
                selectedEdgeKey={selectedEdgeKey}
                edgePickActive={
                  Boolean(edgeRepairTarget) ||
                  addFeatureDraft === "fillet" ||
                  addFeatureDraft === "chamfer"
                }
                onEdgeSelect={(key) => {
                  if (key) {
                    setSelectedEdgeKey(key);
                    setSelectedFaceKey(null);
                    if (edgeRepairTarget) {
                      void handleRepairEdge(edgeRepairTarget, key);
                    } else if (
                      addFeatureDraft === "fillet" ||
                      addFeatureDraft === "chamfer"
                    ) {
                      setPickedEdgeKey(key);
                    }
                  }
                }}
              />
              <AssumptionsStrip
                assumptions={assumptions}
                operations={tree?.operations ?? []}
                onSelectOperation={(id) => {
                  setFocusedAssertionId(null);
                  setSelectedOperationId(id);
                }}
                onRepairEdge={setEdgeRepairTarget}
              />
              {edgeRepairTarget && (
                <div className="shrink-0 border-t border-sky-500/30 bg-sky-500/10 px-3 py-1.5 text-[11px] text-sky-200">
                  Кликните ребро на модели, чтобы задать положение{" "}
                  {edgeRepairTarget.kind === "chamfer" ? "фаски" : "скругления"}{" "}
                  №{edgeRepairTarget.index + 1}.{" "}
                  <button
                    type="button"
                    onClick={() => setEdgeRepairTarget(null)}
                    className="underline decoration-dotted hover:text-white"
                  >
                    Отмена
                  </button>
                </div>
              )}
              {graphRevision && (
                <BlockersAlternativesPanel
                  generationId={generationId}
                  graph={graphRevision.graph}
                  operations={tree?.operations ?? []}
                  onNavigate={(assertionId, operationId) => {
                    setFocusedAssertionId(assertionId);
                    if (operationId) setSelectedOperationId(operationId);
                  }}
                />
              )}
            </>
          )}
          <CadStatusBar
            selectedOperation={selectedOperation}
            operationCount={tree?.operations.length ?? 0}
            needsReviewCount={guessedOperationIds.size}
            hasModel={hasModel}
            incrementalBuild={solid?.kernel_report?.incremental_build}
          />
        </main>

        <ResizablePane
          storageKey="cad-editor:propertiesWidth"
          defaultWidth={384}
          minWidth={280}
          maxWidth={560}
          side="right"
        >
          <aside className="h-full overflow-y-auto border-l border-white/10 bg-zinc-900/60">
            <div className="border-b border-white/10 px-3 py-2 text-[11px] font-medium uppercase tracking-wide text-zinc-400">
              Свойства
            </div>
            {graphRevision && (
              <OperationProvenancePanel
                generationId={generationId}
                graph={graphRevision.graph}
                operation={selectedOperation}
                focusedAssertionId={focusedAssertionId}
              />
            )}
            <TopologyPickerPanel
              faces={faces}
              edges={edges}
              selectedFaceKey={selectedFaceKey}
              selectedEdgeKey={selectedEdgeKey}
              onSelectFace={(key) => {
                setSelectedFaceKey(key);
                if (
                  key &&
                  (addFeatureDraft === "boss" || addFeatureDraft === "pocket")
                ) {
                  const face = faces.find((item) => item.key === key);
                  if (face) {
                    setPickedFaceCenter({
                      x: face.center_of_mass_mm.x,
                      y: face.center_of_mass_mm.y,
                    });
                  }
                }
              }}
              onSelectEdge={(key) => {
                setSelectedEdgeKey(key);
                if (!key) return;
                if (edgeRepairTarget) {
                  void handleRepairEdge(edgeRepairTarget, key);
                } else if (
                  addFeatureDraft === "fillet" ||
                  addFeatureDraft === "chamfer"
                ) {
                  setPickedEdgeKey(key);
                }
              }}
            />
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
              pickedEdgeKey={pickedEdgeKey}
              onEdgeKeyConsumed={() => setPickedEdgeKey(null)}
              pickedFaceCenter={pickedFaceCenter}
              onFaceCenterConsumed={() => setPickedFaceCenter(null)}
              patternDraftActive={patternDraftActive}
              onPatternDraftChange={setPatternDraftActive}
              onSaved={() => void load()}
              onRebuildQueued={(taskId) => {
                setHistoryCursor(null);
                setRebuildTaskId(taskId);
                setRebuildStatus("QUEUED");
              }}
              onError={setError}
            />
          </aside>
        </ResizablePane>
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
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded border border-white/15 px-3 py-1.5 text-xs text-zinc-200 hover:bg-white/5"
      >
        <Download className="h-3.5 w-3.5" /> Экспорт ▾
      </button>
      {open && (
        <div
          className="absolute right-0 z-10 mt-1 w-40 rounded border border-white/10 bg-zinc-900 py-1 shadow-lg"
          onMouseLeave={() => setOpen(false)}
        >
          <a
            href={diagnosticsPackageUrl(generationId)}
            download
            className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-sky-200 hover:bg-sky-500/10"
          >
            <Download className="h-3 w-3" /> Диагностика ZIP
          </a>
          {accepted ? (
            (["dxf", "step", "iges", "stl", "pdf"] as const).map((kind) => (
              <a
                key={kind}
                href={artifactUrl(generationId, kind)}
                download
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/10"
              >
                <ExternalLink className="h-3 w-3" /> {kind.toUpperCase()}
              </a>
            ))
          ) : (
            <p className="border-t border-white/5 px-3 py-1.5 text-[10px] text-zinc-400">
              CAD-экспорт доступен после приёмки
            </p>
          )}
        </div>
      )}
    </div>
  );
}
