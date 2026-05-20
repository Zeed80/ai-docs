"use client";

import { useState, useEffect, use } from "react";
import Link from "next/link";
import clsx from "clsx";

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
  safety_requirements: string | null;
  setup_minutes: number | null;
  machine_minutes: number | null;
  labor_minutes: number | null;
}

interface ProcessPlanDetail {
  id: string;
  product_name: string;
  product_code: string | null;
  version: string;
  status: string;
  standard_system: string;
  material: string | null;
  blank_type: string | null;
  route_summary: string | null;
  quality_requirements: string | null;
  created_by: string;
  created_at: string;
  approved_at: string | null;
  approved_by: string | null;
  operations: Operation[];
}

const STATUS_COLORS: Record<string, string> = {
  draft: "text-yellow-400 bg-yellow-400/10",
  approved: "text-emerald-400 bg-emerald-400/10",
  obsolete: "text-zinc-500 bg-zinc-500/10",
};

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  approved: "Утверждена",
  obsolete: "Устарела",
};

export default function TechCardPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [plan, setPlan] = useState<ProcessPlanDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<"route" | "ops">("route");

  useEffect(() => {
    fetch(`/api/technology/process-plans/${id}`)
      .then((r) => r.json())
      .then((data) => setPlan(data.process_plan ?? data))
      .catch(() => setPlan(null))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading)
    return (
      <div className="p-8 text-white/30 text-sm">Загрузка техкарты...</div>
    );
  if (!plan)
    return (
      <div className="p-8 text-red-400 text-sm">
        Техкарта не найдена.{" "}
        <Link href="/technology" className="underline">
          Вернуться
        </Link>
      </div>
    );

  const totalSetup = plan.operations.reduce(
    (s, o) => s + (o.setup_minutes ?? 0),
    0,
  );
  const totalMachine = plan.operations.reduce(
    (s, o) => s + (o.machine_minutes ?? 0),
    0,
  );
  const totalLabor = plan.operations.reduce(
    (s, o) => s + (o.labor_minutes ?? 0),
    0,
  );

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
            <span>Стандарт: {plan.standard_system}</span>
            {plan.approved_by && (
              <span className="text-emerald-400/70">
                Утвердил: {plan.approved_by}
              </span>
            )}
          </div>
        </div>

        {/* Export buttons */}
        <div className="flex gap-2 shrink-0">
          <a
            href={`/api/technology/process-plans/${id}/export?format=excel`}
            className="px-3 py-1.5 bg-emerald-700/40 hover:bg-emerald-700/60 text-emerald-300 rounded text-xs font-medium transition-colors"
          >
            ↓ Excel (ТК)
          </a>
          <a
            href={`/api/technology/process-plans/${id}/export?format=html`}
            target="_blank"
            rel="noreferrer"
            className="px-3 py-1.5 bg-blue-700/40 hover:bg-blue-700/60 text-blue-300 rounded text-xs font-medium transition-colors"
          >
            🖨 МК печать
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
            value: `${totalLabor.toFixed(1)} мин`,
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

      {plan.route_summary && (
        <div className="bg-zinc-900 border border-white/10 rounded-xl p-4 mb-6">
          <div className="text-white/30 text-xs mb-1">Маршрут</div>
          <div className="text-white/80 text-sm">{plan.route_summary}</div>
        </div>
      )}

      {/* View toggle */}
      <div className="flex items-center gap-1 mb-4 bg-zinc-900 border border-white/10 rounded-lg p-1 w-fit">
        {(["route", "ops"] as const).map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={clsx(
              "px-4 py-1.5 rounded text-sm font-medium transition-colors",
              view === v
                ? "bg-blue-600 text-white"
                : "text-white/40 hover:text-white",
            )}
          >
            {v === "route" ? "Маршрутная карта" : "Операции (таблица)"}
          </button>
        ))}
      </div>

      {view === "route" ? (
        <RouteCardView operations={plan.operations} />
      ) : (
        <OperationsTable
          operations={plan.operations}
          totals={{
            setup: totalSetup,
            machine: totalMachine,
            labor: totalLabor,
          }}
        />
      )}
    </div>
  );
}

function RouteCardView({ operations }: { operations: Operation[] }) {
  if (operations.length === 0)
    return (
      <div className="text-white/20 text-sm py-8 text-center">
        Операции не добавлены
      </div>
    );

  return (
    <div className="space-y-2">
      {operations.map((op, idx) => (
        <div
          key={op.id}
          className="flex gap-4 bg-zinc-900 border border-white/10 rounded-xl p-4 hover:border-white/20 transition-colors"
        >
          {/* Step indicator */}
          <div className="flex flex-col items-center shrink-0">
            <div className="w-8 h-8 rounded-full bg-blue-600/20 border border-blue-500/40 flex items-center justify-center text-blue-300 font-bold text-sm">
              {op.sequence_no}
            </div>
            {idx < operations.length - 1 && (
              <div className="w-px flex-1 bg-white/10 my-1 min-h-[12px]" />
            )}
          </div>

          {/* Content */}
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
              <p className="text-white/50 text-sm mb-2">
                {op.setup_description}
              </p>
            )}
            {op.transition_text && (
              <p className="text-white/40 text-xs mb-2 italic">
                {op.transition_text}
              </p>
            )}

            <div className="flex flex-wrap gap-4 mt-2">
              {op.setup_minutes != null && (
                <TimeChip label="t пз" value={op.setup_minutes} />
              )}
              {op.machine_minutes != null && (
                <TimeChip label="t оп" value={op.machine_minutes} />
              )}
              {op.labor_minutes != null && (
                <TimeChip label="t труд" value={op.labor_minutes} />
              )}
              {op.control_requirements && (
                <span className="text-xs text-amber-400/60">
                  ✓ {op.control_requirements}
                </span>
              )}
            </div>

            {op.cutting_parameters &&
              Object.keys(op.cutting_parameters).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {Object.entries(op.cutting_parameters).map(([k, v]) => (
                    <span
                      key={k}
                      className="text-xs bg-zinc-800 border border-white/10 text-white/50 px-2 py-0.5 rounded font-mono"
                    >
                      {k}: {String(v)}
                    </span>
                  ))}
                </div>
              )}
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
      <span className="text-white/60 font-mono">{value.toFixed(1)} мин</span>
    </span>
  );
}

function OperationsTable({
  operations,
  totals,
}: {
  operations: Operation[];
  totals: { setup: number; machine: number; labor: number };
}) {
  if (operations.length === 0)
    return (
      <div className="text-white/20 text-sm py-8 text-center">
        Операции не добавлены
      </div>
    );

  return (
    <div className="bg-zinc-900 border border-white/10 rounded-xl overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-white/10">
            {[
              "№",
              "Код",
              "Наименование",
              "Тип",
              "Переход",
              "t пз",
              "t оп",
              "t труд",
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
          {operations.map((op) => (
            <tr
              key={op.id}
              className="border-b border-white/5 hover:bg-zinc-800/50 transition-colors"
            >
              <td className="px-3 py-2 text-white/50 text-center font-mono">
                {op.sequence_no}
              </td>
              <td className="px-3 py-2 font-mono text-white/40 text-xs">
                {op.operation_code || "—"}
              </td>
              <td className="px-3 py-2 text-white font-medium max-w-xs">
                {op.name}
              </td>
              <td className="px-3 py-2 text-white/40 text-xs">
                {op.operation_type || "—"}
              </td>
              <td className="px-3 py-2 text-white/40 text-xs max-w-xs truncate">
                {op.transition_text || "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono text-white/50 text-xs">
                {op.setup_minutes?.toFixed(1) ?? "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono text-white/50 text-xs">
                {op.machine_minutes?.toFixed(1) ?? "—"}
              </td>
              <td className="px-3 py-2 text-right font-mono text-white/50 text-xs">
                {op.labor_minutes?.toFixed(1) ?? "—"}
              </td>
              <td className="px-3 py-2 text-white/30 text-xs max-w-[180px] truncate">
                {op.control_requirements || "—"}
              </td>
            </tr>
          ))}
          {/* Totals */}
          <tr className="border-t border-white/20 bg-zinc-800/30">
            <td
              colSpan={5}
              className="px-3 py-2 text-white/40 text-xs font-semibold text-right"
            >
              ИТОГО (мин):
            </td>
            <td className="px-3 py-2 text-right font-mono text-white/70 text-xs font-semibold">
              {totals.setup.toFixed(1)}
            </td>
            <td className="px-3 py-2 text-right font-mono text-white/70 text-xs font-semibold">
              {totals.machine.toFixed(1)}
            </td>
            <td className="px-3 py-2 text-right font-mono text-white/70 text-xs font-semibold">
              {totals.labor.toFixed(1)}
            </td>
            <td />
          </tr>
        </tbody>
      </table>
    </div>
  );
}
