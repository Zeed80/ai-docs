"use client";

import { useState, useEffect, use, useCallback } from "react";
import Link from "next/link";
import clsx from "clsx";
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

interface Operation {
  id: string;
  sequence_no: number;
  operation_code: string | null;
  name: string;
  operation_type: string | null;
  setup_description: string | null;
  transition_text: string | null;
  cutting_parameters: Record<string, unknown> | null;
  control_requirements: string | null;
  setup_minutes: number | null;
  machine_minutes: number | null;
  labor_minutes: number | null;
  to_minutes: number | null;
  tsht_minutes: number | null;
  tsht_k_minutes: number | null;
  tpz_minutes: number | null;
}

interface ProcessPlanDetail {
  id: string;
  product_name: string;
  product_code: string | null;
  version: string;
  status: string;
  tp_type: string | null;
  standard_system: string;
  material: string | null;
  blank_type: string | null;
  route_summary: string | null;
  quality_requirements: string | null;
  normcontrol_status: string;
  total_norm_minutes: number | null;
  drawing_id: string | null;
  created_by: string;
  created_at: string;
  approved_at: string | null;
  approved_by: string | null;
  operations: Operation[];
}

interface NormcontrolResult {
  status: "passed" | "failed" | "not_checked" | "checking";
  checks: NormControlCheck[];
  errors_count: number;
  warnings_count: number;
  total_count: number;
}

type Tab = "route" | "ops" | "surfaces" | "normcontrol" | "export";

const TAB_LABELS: Record<Tab, string> = {
  route: "Маршрут",
  ops: "Операции",
  surfaces: "Поверхности",
  normcontrol: "Нормоконтроль",
  export: "Формы ГОСТ",
};

const STATUS_COLORS: Record<string, string> = {
  draft: "text-yellow-400 bg-yellow-400/10",
  approved: "text-emerald-400 bg-emerald-400/10",
  obsolete: "text-zinc-500 bg-zinc-500/10",
};

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  approved: "Утверждён",
  obsolete: "Устарел",
};

export default function TechPlanPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [plan, setPlan] = useState<ProcessPlanDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<Tab>("route");

  const [surfaces, setSurfaces] = useState<SurfaceSpec[]>([]);
  const [blankSpec, setBlankSpec] = useState<BlankSpec | null>(null);
  const [normcontrol, setNormcontrol] = useState<NormcontrolResult | null>(
    null,
  );
  const [selectedOpId, setSelectedOpId] = useState<string | null>(null);

  const fetchPlan = useCallback(async () => {
    const r = await fetch(`/api/technology/process-plans/${id}`);
    if (!r.ok) return;
    const data = await r.json();
    setPlan(data.process_plan ?? data);
  }, [id]);

  const fetchSurfaces = useCallback(async () => {
    const r = await fetch(`/api/technology/process-plans/${id}/surface-specs`);
    if (!r.ok) return;
    const data = await r.json();
    setSurfaces(data.items ?? []);
  }, [id]);

  const fetchBlankSpec = useCallback(async () => {
    const r = await fetch(`/api/technology/process-plans/${id}/blank-spec`);
    if (r.ok) setBlankSpec(await r.json());
  }, [id]);

  const fetchNormcontrol = useCallback(async () => {
    const r = await fetch(
      `/api/technology/process-plans/${id}/normcontrol-result`,
    );
    if (r.ok) setNormcontrol(await r.json());
  }, [id]);

  useEffect(() => {
    setLoading(true);
    Promise.all([fetchPlan(), fetchSurfaces(), fetchBlankSpec()]).finally(() =>
      setLoading(false),
    );
  }, [fetchPlan, fetchSurfaces, fetchBlankSpec]);

  useEffect(() => {
    if (!plan) return;
    if (
      plan.normcontrol_status !== "not_checked" &&
      plan.normcontrol_status !== "checking"
    ) {
      fetchNormcontrol();
    }
  }, [plan, fetchNormcontrol]);

  const handleRunNormcontrol = async () => {
    setNormcontrol({
      status: "checking",
      checks: [],
      errors_count: 0,
      warnings_count: 0,
      total_count: 0,
    });
    try {
      const r = await fetch(`/api/technology/process-plans/${id}/normcontrol`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (r.ok) {
        setNormcontrol(await r.json());
        await fetchPlan();
      }
    } catch {}
  };

  const handleResolveCheck = async (
    checkId: string,
    resolution: "fixed" | "waived",
  ) => {
    const r = await fetch(
      `/api/technology/process-plans/${id}/normcontrol/${checkId}/resolve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution }),
      },
    );
    if (r.ok && normcontrol) {
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

  if (loading)
    return (
      <div className="p-8 text-white/30 text-sm">Загрузка техпроцесса...</div>
    );
  if (!plan)
    return (
      <div className="p-8 text-red-400 text-sm">
        Технологический процесс не найден.{" "}
        <Link href="/technology" className="underline">
          Вернуться
        </Link>
      </div>
    );

  const totalLabor = plan.operations.reduce(
    (s, o) => s + (o.labor_minutes ?? o.tsht_minutes ?? 0),
    0,
  );
  const ncStatus = normcontrol?.status ?? plan.normcontrol_status;

  return (
    <div className="p-6 max-w-7xl mx-auto">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-xs text-white/30 mb-4">
        <Link
          href="/technology"
          className="hover:text-white/60 transition-colors"
        >
          Технология
        </Link>
        <span>/</span>
        <span className="text-white/60">{plan.product_name}</span>
      </div>

      {/* Header */}
      <div className="flex items-start justify-between mb-6 gap-4">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <h1 className="text-xl font-bold text-white">
              {plan.product_name}
            </h1>
            <span
              className={clsx(
                "text-xs px-2 py-0.5 rounded-full font-medium",
                STATUS_COLORS[plan.status] || "text-white/40 bg-white/5",
              )}
            >
              {STATUS_LABELS[plan.status] || plan.status}
            </span>
            {plan.tp_type && (
              <span className="text-xs px-2 py-0.5 rounded bg-zinc-700 text-zinc-300">
                {plan.tp_type}
              </span>
            )}
          </div>
          <div className="flex flex-wrap gap-4 text-xs text-white/40">
            {plan.product_code && (
              <span>
                Код:{" "}
                <span className="font-mono text-white/60">
                  {plan.product_code}
                </span>
              </span>
            )}
            <span>Версия: {plan.version}</span>
            {plan.total_norm_minutes != null && (
              <span>
                Σ Тшт-к:{" "}
                <span className="font-mono text-white/70">
                  {plan.total_norm_minutes} мин
                </span>
              </span>
            )}
            {plan.approved_by && (
              <span className="text-emerald-400/70">
                Утвердил: {plan.approved_by}
              </span>
            )}
          </div>
        </div>

        <div className="flex gap-2 shrink-0">
          <Link
            href={`/technology/${id}/review`}
            className="px-3 py-1.5 bg-blue-700/40 hover:bg-blue-700/60 text-blue-300 rounded text-xs font-medium transition-colors"
          >
            Открыть редактор →
          </Link>
          <a
            href={`/api/technology/process-plans/${id}/export?format=excel`}
            className="px-3 py-1.5 bg-emerald-700/40 hover:bg-emerald-700/60 text-emerald-300 rounded text-xs font-medium transition-colors"
          >
            ↓ Excel
          </a>
        </div>
      </div>

      {/* Meta cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        {[
          { label: "Материал", value: plan.material },
          { label: "Заготовка", value: plan.blank_type },
          { label: "Операций", value: plan.operations.length.toString() },
          {
            label: "Итого t труд",
            value: totalLabor > 0 ? `${totalLabor.toFixed(1)} мин` : "—",
          },
        ].map(({ label, value }) => (
          <div
            key={label}
            className="bg-zinc-900 border border-white/10 rounded-xl p-4"
          >
            <div className="text-white/30 text-xs mb-1">{label}</div>
            <div className="text-white font-semibold">{value || "—"}</div>
          </div>
        ))}
      </div>

      {blankSpec && (
        <div className="mb-4">
          <BlankSpecCard spec={blankSpec} />
        </div>
      )}

      {/* Tab bar */}
      <div className="flex items-center gap-1 mb-4 border-b border-white/10">
        {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={clsx(
              "px-4 py-2 text-xs font-medium border-b-2 -mb-px transition-colors",
              tab === t
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-white/40 hover:text-white/70",
            )}
          >
            {TAB_LABELS[t]}
            {t === "normcontrol" &&
              normcontrol &&
              normcontrol.errors_count > 0 && (
                <span className="ml-1.5 bg-red-600 text-white text-xs px-1 rounded-full">
                  {normcontrol.errors_count}
                </span>
              )}
            {t === "surfaces" && surfaces.length > 0 && (
              <span className="ml-1.5 text-white/20">{surfaces.length}</span>
            )}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {tab === "route" && (
        <div>
          {plan.route_summary && (
            <div className="bg-zinc-900 border border-white/10 rounded-xl p-4 mb-4">
              <div className="text-white/30 text-xs mb-1">Маршрут</div>
              <div className="text-white/80 text-sm">{plan.route_summary}</div>
            </div>
          )}
          <RouteCardView
            operations={plan.operations}
            selectedOpId={selectedOpId}
            onSelectOp={setSelectedOpId}
          />
        </div>
      )}

      {tab === "ops" && <OperationsTable operations={plan.operations} />}

      {tab === "surfaces" && (
        <SurfaceSpecTable
          specs={surfaces}
          selectedOperationId={selectedOpId}
          onSelectSurface={(s) => {
            if (s.operation_id) setSelectedOpId(s.operation_id);
          }}
        />
      )}

      {tab === "normcontrol" && (
        <NormControlPanel
          planId={id}
          result={normcontrol}
          onRerun={handleRunNormcontrol}
          onResolve={handleResolveCheck}
        />
      )}

      {tab === "export" && (
        <GostFormsExporter
          planId={id}
          productCode={plan.product_code}
          version={plan.version}
          normcontrolStatus={ncStatus}
        />
      )}
    </div>
  );
}

function RouteCardView({
  operations,
  selectedOpId,
  onSelectOp,
}: {
  operations: Operation[];
  selectedOpId: string | null;
  onSelectOp: (id: string) => void;
}) {
  if (operations.length === 0)
    return (
      <div className="text-white/20 text-sm py-8 text-center">
        Операции не добавлены
      </div>
    );

  const sorted = [...operations].sort((a, b) => a.sequence_no - b.sequence_no);

  return (
    <div className="space-y-2">
      {sorted.map((op, idx) => (
        <div
          key={op.id}
          onClick={() => onSelectOp(op.id)}
          className={clsx(
            "flex gap-4 bg-zinc-900 border rounded-xl p-4 cursor-pointer transition-colors",
            selectedOpId === op.id
              ? "border-blue-500/50 bg-blue-900/10"
              : "border-white/10 hover:border-white/20",
          )}
        >
          <div className="flex flex-col items-center shrink-0">
            <div className="w-8 h-8 rounded-full bg-blue-600/20 border border-blue-500/40 flex items-center justify-center text-blue-300 font-bold text-sm">
              {op.sequence_no}
            </div>
            {idx < sorted.length - 1 && (
              <div className="w-px flex-1 bg-white/10 my-1 min-h-[12px]" />
            )}
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-baseline gap-2 mb-1">
              <span className="font-semibold text-white">{op.name}</span>
              {op.operation_code && (
                <span className="font-mono text-white/30 text-xs">
                  {op.operation_code}
                </span>
              )}
              {op.operation_type && (
                <span className="text-xs text-blue-400/60 bg-blue-500/10 px-1.5 py-0.5 rounded">
                  {op.operation_type}
                </span>
              )}
            </div>

            {op.setup_description && (
              <p className="text-white/50 text-sm mb-1">
                {op.setup_description}
              </p>
            )}
            {op.transition_text && (
              <p className="text-white/40 text-xs mb-2 italic">
                {op.transition_text}
              </p>
            )}

            <div className="flex flex-wrap gap-4 mt-1">
              {op.to_minutes != null && (
                <TimeChip label="То" value={op.to_minutes} />
              )}
              {op.tsht_minutes != null && (
                <TimeChip label="Тшт" value={op.tsht_minutes} />
              )}
              {op.tsht_k_minutes != null && (
                <TimeChip label="Тшт-к" value={op.tsht_k_minutes} />
              )}
              {op.control_requirements && (
                <span className="text-xs text-amber-400/60">
                  ✓ {op.control_requirements}
                </span>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function TimeChip({ label, value }: { label: string; value: number }) {
  return (
    <span className="flex items-center gap-1 text-xs text-white/40">
      <span className="text-white/20">{label}</span>
      <span className="text-white/60 font-mono">{value.toFixed(2)} мин</span>
    </span>
  );
}

function OperationsTable({ operations }: { operations: Operation[] }) {
  if (operations.length === 0)
    return (
      <div className="text-white/20 text-sm py-8 text-center">
        Операции не добавлены
      </div>
    );

  const sorted = [...operations].sort((a, b) => a.sequence_no - b.sequence_no);

  return (
    <div className="bg-zinc-900 border border-white/10 rounded-xl overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-white/10">
            {[
              "№",
              "Код",
              "Операция",
              "Тип",
              "То",
              "Тшт",
              "Тшт-к",
              "Тпз",
              "Контроль",
            ].map((h) => (
              <th
                key={h}
                className="text-left px-3 py-3 text-white/30 font-medium whitespace-nowrap"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((op) => (
            <tr
              key={op.id}
              className="border-b border-white/5 hover:bg-zinc-800/50 transition-colors"
            >
              <td className="px-3 py-2 text-white/50 font-mono">
                {String(op.sequence_no).padStart(3, "0")}
              </td>
              <td className="px-3 py-2 font-mono text-white/40">
                {op.operation_code || "—"}
              </td>
              <td className="px-3 py-2 text-white font-medium max-w-[200px]">
                {op.name}
              </td>
              <td className="px-3 py-2 text-white/40">
                {op.operation_type || "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono text-white/50">
                {op.to_minutes?.toFixed(2) ?? "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono text-white/50">
                {op.tsht_minutes?.toFixed(2) ?? "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono text-white/70 font-medium">
                {op.tsht_k_minutes?.toFixed(2) ?? "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono text-white/40">
                {op.tpz_minutes?.toFixed(2) ?? "—"}
              </td>
              <td className="px-3 py-2 text-white/30 max-w-[160px] truncate">
                {op.control_requirements || "—"}
              </td>
            </tr>
          ))}
          <tr className="border-t border-white/20 bg-zinc-800/30">
            <td
              colSpan={4}
              className="px-3 py-2 text-white/40 text-right font-semibold"
            >
              Итого (мин):
            </td>
            <td className="px-3 py-2 text-right font-mono text-white/70 font-semibold">
              {sorted.reduce((s, o) => s + (o.to_minutes ?? 0), 0).toFixed(2)}
            </td>
            <td className="px-3 py-2 text-right font-mono text-white/70 font-semibold">
              {sorted.reduce((s, o) => s + (o.tsht_minutes ?? 0), 0).toFixed(2)}
            </td>
            <td className="px-3 py-2 text-right font-mono text-white/70 font-semibold">
              {sorted
                .reduce((s, o) => s + (o.tsht_k_minutes ?? 0), 0)
                .toFixed(2)}
            </td>
            <td colSpan={2} />
          </tr>
        </tbody>
      </table>
    </div>
  );
}
