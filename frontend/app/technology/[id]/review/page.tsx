"use client";

import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import TechProcessEditor, {
  Operation,
} from "@/components/technology/TechProcessEditor";
import NormControlPanel, {
  NormControlCheck,
} from "@/components/technology/NormControlPanel";
import BlankSpecCard, {
  BlankSpec,
} from "@/components/technology/BlankSpecCard";
import SurfaceSpecTable, {
  SurfaceSpec,
} from "@/components/technology/SurfaceSpecTable";
import GostFormsExporter from "@/components/technology/GostFormsExporter";

interface ProcessPlan {
  id: string;
  product_name: string;
  product_code: string | null;
  version: string;
  status: string;
  tp_type: string;
  material: string | null;
  blank_type: string | null;
  route_summary: string | null;
  normcontrol_status: string;
  total_norm_minutes: number | null;
  drawing_id: string | null;
  operations: Operation[];
}

interface NormcontrolResult {
  status: "passed" | "failed" | "not_checked" | "checking";
  checks: NormControlCheck[];
  errors_count: number;
  warnings_count: number;
  total_count: number;
}

type ActiveTab = "operations" | "surfaces" | "normcontrol" | "export";

const TAB_LABELS: Record<ActiveTab, string> = {
  operations: "Операции",
  surfaces: "Поверхности",
  normcontrol: "Нормоконтроль",
  export: "Формы ГОСТ",
};

export default function TechProcessReviewPage() {
  const { id: planId } = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const taskId = searchParams.get("task_id");

  const [plan, setPlan] = useState<ProcessPlan | null>(null);
  const [blankSpec, setBlankSpec] = useState<BlankSpec | null>(null);
  const [surfaces, setSurfaces] = useState<SurfaceSpec[]>([]);
  const [normcontrol, setNormcontrol] = useState<NormcontrolResult | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [taskStatus, setTaskStatus] = useState<string | null>(
    taskId ? "running" : null,
  );

  const [activeTab, setActiveTab] = useState<ActiveTab>("operations");
  const [selectedOpId, setSelectedOpId] = useState<string | null>(null);
  const [rightPanelWidth, setRightPanelWidth] = useState(55);

  const fetchPlan = useCallback(async () => {
    try {
      const res = await fetch(`/api/technology/process-plans/${planId}`);
      if (!res.ok) return;
      const data = await res.json();
      setPlan(data.process_plan ?? data);
    } catch {}
  }, [planId]);

  const fetchSurfaces = useCallback(async () => {
    try {
      const res = await fetch(
        `/api/technology/process-plans/${planId}/surface-specs`,
      );
      if (!res.ok) return;
      const data = await res.json();
      setSurfaces(data.items ?? []);
    } catch {}
  }, [planId]);

  const fetchBlankSpec = useCallback(async () => {
    try {
      const res = await fetch(
        `/api/technology/process-plans/${planId}/blank-spec`,
      );
      if (res.ok) {
        setBlankSpec(await res.json());
      }
    } catch {}
  }, [planId]);

  // Poll task status while generation is running
  useEffect(() => {
    if (!taskId || taskStatus !== "running") return;
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/tasks/${taskId}/status`);
        if (res.ok) {
          const data = await res.json();
          if (data.status === "SUCCESS" || data.status === "completed") {
            setTaskStatus("done");
            await fetchPlan();
            await fetchSurfaces();
            await fetchBlankSpec();
          } else if (data.status === "FAILURE") {
            setTaskStatus("failed");
          }
        }
      } catch {}
    }, 2500);
    return () => clearInterval(interval);
  }, [taskId, taskStatus, fetchPlan, fetchSurfaces, fetchBlankSpec]);

  useEffect(() => {
    setLoading(true);
    Promise.all([fetchPlan(), fetchSurfaces(), fetchBlankSpec()]).finally(() =>
      setLoading(false),
    );
  }, [fetchPlan, fetchSurfaces, fetchBlankSpec]);

  // Load normcontrol result from plan status
  useEffect(() => {
    if (!plan) return;
    if (
      plan.normcontrol_status !== "not_checked" &&
      plan.normcontrol_status !== "checking"
    ) {
      fetch(`/api/technology/process-plans/${planId}/normcontrol-result`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (data) setNormcontrol(data);
        })
        .catch(() => {});
    }
  }, [plan, planId]);

  const handleRunNormcontrol = async () => {
    setNormcontrol({
      status: "checking",
      checks: [],
      errors_count: 0,
      warnings_count: 0,
      total_count: 0,
    });
    try {
      const res = await fetch(
        `/api/technology/process-plans/${planId}/normcontrol`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        },
      );
      if (res.ok) {
        const data = await res.json();
        setNormcontrol(data);
        await fetchPlan();
      }
    } catch {}
  };

  const handleResolveCheck = async (
    checkId: string,
    resolution: "fixed" | "waived",
  ) => {
    const res = await fetch(
      `/api/technology/process-plans/${planId}/normcontrol/${checkId}/resolve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution }),
      },
    );
    if (res.ok && normcontrol) {
      setNormcontrol({
        ...normcontrol,
        checks: normcontrol.checks.map((c) =>
          c.id === checkId
            ? { ...c, status: resolution === "fixed" ? "resolved" : "waived" }
            : c,
        ),
      });
    }
  };

  const handleUpdateOperation = async (
    id: string,
    field: string,
    value: unknown,
  ) => {
    await fetch(`/api/technology/operations/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    await fetchPlan();
  };

  const handleApprove = async () => {
    const res = await fetch(`/api/technology/process-plans/${planId}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved_by: "user" }),
    });
    if (res.ok) await fetchPlan();
  };

  if (loading) {
    return (
      <div className="min-h-screen bg-zinc-950 flex items-center justify-center text-zinc-400">
        Загрузка…
      </div>
    );
  }

  if (!plan) {
    return (
      <div className="min-h-screen bg-zinc-950 flex items-center justify-center text-red-400">
        Технологический процесс не найден.
      </div>
    );
  }

  const ncStatus = normcontrol?.status ?? plan.normcontrol_status;

  return (
    <div className="h-screen bg-zinc-950 text-zinc-100 flex flex-col overflow-hidden">
      {/* Top bar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-zinc-800 bg-zinc-900 flex-shrink-0">
        <div className="flex items-center gap-3">
          <Link
            href="/technology"
            className="text-xs text-zinc-500 hover:text-zinc-300"
          >
            ← Техпроцессы
          </Link>
          <span className="text-zinc-700">/</span>
          <span className="text-sm font-medium text-zinc-200 truncate max-w-xs">
            {plan.product_name}
          </span>
          <span className="text-xs text-zinc-500">{plan.product_code}</span>
          <span className="text-xs bg-zinc-700 px-1.5 py-0.5 rounded text-zinc-300">
            {plan.tp_type}
          </span>
          <span
            className={`text-xs px-1.5 py-0.5 rounded ${
              plan.status === "approved"
                ? "bg-emerald-900/50 text-emerald-400"
                : "bg-yellow-900/50 text-yellow-400"
            }`}
          >
            {plan.status === "approved" ? "Утверждён" : "Черновик"}
          </span>
        </div>

        <div className="flex items-center gap-2">
          {/* Generation task status */}
          {taskStatus === "running" && (
            <span className="text-xs text-blue-400 flex items-center gap-1">
              <span className="animate-spin text-sm">⟳</span> Генерация…
            </span>
          )}
          {taskStatus === "failed" && (
            <span className="text-xs text-red-400">⚠ Ошибка генерации</span>
          )}

          {/* Normcontrol badge */}
          <span
            className={`text-xs px-2 py-0.5 rounded ${
              ncStatus === "passed"
                ? "bg-emerald-900/50 text-emerald-400"
                : ncStatus === "failed"
                  ? "bg-red-900/50 text-red-400"
                  : ncStatus === "checking"
                    ? "bg-blue-900/50 text-blue-400"
                    : "bg-zinc-700 text-zinc-400"
            }`}
          >
            НК:{" "}
            {ncStatus === "passed"
              ? "✓"
              : ncStatus === "failed"
                ? "✗"
                : ncStatus === "checking"
                  ? "…"
                  : "—"}
          </span>

          {plan.total_norm_minutes !== null && (
            <span className="text-xs text-zinc-500">
              Σ Тшт-к ={" "}
              <span className="text-zinc-300 font-mono">
                {plan.total_norm_minutes} мин
              </span>
            </span>
          )}

          {plan.status !== "approved" && (
            <button
              onClick={handleApprove}
              disabled={ncStatus === "failed"}
              className="text-xs px-3 py-1 rounded bg-emerald-700 hover:bg-emerald-600 text-white disabled:opacity-40 transition"
              title={
                ncStatus === "failed" ? "Исправьте замечания нормоконтроля" : ""
              }
            >
              Утвердить ТП
            </button>
          )}
        </div>
      </div>

      {/* Main two-panel layout */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left panel: Drawing info / route summary */}
        <div
          className="flex-shrink-0 border-r border-zinc-800 overflow-y-auto bg-zinc-900"
          style={{ width: `${100 - rightPanelWidth}%` }}
        >
          <div className="p-4 space-y-4">
            {/* Plan header */}
            <div className="space-y-1">
              <p className="text-xs text-zinc-500">Материал</p>
              <p className="text-sm text-zinc-200">{plan.material ?? "—"}</p>
            </div>

            {blankSpec && <BlankSpecCard spec={blankSpec} />}

            {plan.route_summary && (
              <div>
                <p className="text-xs text-zinc-500 mb-1">Маршрут</p>
                <p className="text-xs text-zinc-400 leading-relaxed">
                  {plan.route_summary}
                </p>
              </div>
            )}

            {/* Drawing preview placeholder */}
            {plan.drawing_id && (
              <div className="rounded-lg border border-zinc-700 bg-zinc-800/50 p-3">
                <div className="flex items-center justify-between mb-2">
                  <p className="text-xs text-zinc-400">Чертёж</p>
                  <a
                    href={`/drawings/${plan.drawing_id}`}
                    className="text-xs text-blue-400 hover:underline"
                  >
                    Открыть →
                  </a>
                </div>
                <div className="h-40 flex items-center justify-center text-zinc-600 text-xs border border-zinc-700 rounded">
                  Предпросмотр чертежа
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Right panel: tabs */}
        <div
          className="flex-1 overflow-hidden flex flex-col"
          style={{ width: `${rightPanelWidth}%` }}
        >
          {/* Tab bar */}
          <div className="flex items-center border-b border-zinc-800 bg-zinc-900 flex-shrink-0">
            {(Object.keys(TAB_LABELS) as ActiveTab[]).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-4 py-2.5 text-xs font-medium transition border-b-2 ${
                  activeTab === tab
                    ? "border-blue-500 text-blue-400"
                    : "border-transparent text-zinc-500 hover:text-zinc-300"
                }`}
              >
                {TAB_LABELS[tab]}
                {tab === "normcontrol" &&
                  normcontrol &&
                  normcontrol.errors_count > 0 && (
                    <span className="ml-1.5 bg-red-600 text-white text-xs px-1 rounded-full">
                      {normcontrol.errors_count}
                    </span>
                  )}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-y-auto">
            {activeTab === "operations" && (
              <TechProcessEditor
                operations={plan.operations}
                selectedOperationId={selectedOpId}
                onSelectOperation={(op) => setSelectedOpId(op.id)}
                onUpdateOperation={handleUpdateOperation}
              />
            )}

            {activeTab === "surfaces" && (
              <div className="p-2">
                <SurfaceSpecTable
                  specs={surfaces}
                  selectedOperationId={selectedOpId}
                  onSelectSurface={(s) => {
                    if (s.operation_id) setSelectedOpId(s.operation_id);
                  }}
                />
              </div>
            )}

            {activeTab === "normcontrol" && (
              <NormControlPanel
                planId={planId}
                result={normcontrol}
                onRerun={handleRunNormcontrol}
                onResolve={handleResolveCheck}
              />
            )}

            {activeTab === "export" && (
              <div className="p-4 space-y-4">
                <GostFormsExporter
                  planId={planId}
                  productCode={plan.product_code}
                  version={plan.version}
                  normcontrolStatus={plan.normcontrol_status}
                />
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
