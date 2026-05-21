"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import clsx from "clsx";

interface ProcessPlan {
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
  normcontrol_status: string | null;
  total_norm_minutes: number | null;
  created_by: string;
  created_at: string;
  approved_at: string | null;
  approved_by: string | null;
}

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  approved: "Утверждена",
  obsolete: "Устарела",
};

const STATUS_COLORS: Record<string, string> = {
  draft: "text-yellow-400",
  approved: "text-emerald-400",
  obsolete: "text-zinc-500",
};

const NC_STATUS: Record<string, { label: string; cls: string }> = {
  passed: { label: "✓", cls: "text-emerald-400" },
  failed: { label: "✗", cls: "text-red-400" },
  checking: { label: "…", cls: "text-blue-400" },
  not_checked: { label: "—", cls: "text-white/20" },
};

function NcBadge({ status }: { status: string | null }) {
  const s = NC_STATUS[status ?? "not_checked"] ?? NC_STATUS["not_checked"];
  return (
    <span className={clsx("text-xs font-mono font-medium", s.cls)}>
      {s.label}
    </span>
  );
}

export default function TechnologyPage() {
  const [plans, setPlans] = useState<ProcessPlan[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const PAGE_SIZE = 20;

  const load = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(PAGE_SIZE),
      });
      if (search) params.set("product_name", search);
      if (statusFilter) params.set("status", statusFilter);
      const r = await fetch(`/api/technology/process-plans?${params}`);
      if (!r.ok) throw new Error("fetch failed");
      const data = await r.json();
      setPlans(data.items || []);
      setTotal(data.total || 0);
    } catch {
      setPlans([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, [page, search, statusFilter]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">
            Технологические процессы
          </h1>
          <p className="text-white/40 text-sm mt-1">
            {total > 0 ? `${total} ТП` : "Нет технологических процессов"}
          </p>
        </div>
        <div className="flex gap-2">
          <Link
            href="/technology/new"
            className="px-4 py-2 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg text-sm font-medium transition-colors"
          >
            + Создать из чертежа
          </Link>
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-5">
        <input
          type="text"
          placeholder="Поиск по изделию..."
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(1);
          }}
          className="flex-1 bg-zinc-800 border border-white/10 text-white rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500/60"
        />
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value);
            setPage(1);
          }}
          className="bg-zinc-800 border border-white/10 text-white/70 rounded-lg px-3 py-2 text-sm focus:outline-none"
        >
          <option value="">Все статусы</option>
          <option value="draft">Черновик</option>
          <option value="approved">Утверждена</option>
          <option value="obsolete">Устарела</option>
        </select>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-white/30 text-sm py-12 text-center">
          Загрузка...
        </div>
      ) : plans.length === 0 ? (
        <div className="text-white/30 text-sm py-12 text-center">
          Техкарты не найдены
        </div>
      ) : (
        <div className="bg-zinc-900 border border-white/10 rounded-xl overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-white/10">
                <th className="text-left px-4 py-3 text-white/40 font-medium">
                  Изделие
                </th>
                <th className="text-left px-4 py-3 text-white/40 font-medium">
                  Код
                </th>
                <th className="text-left px-4 py-3 text-white/40 font-medium">
                  Тип ТП
                </th>
                <th className="text-left px-4 py-3 text-white/40 font-medium">
                  Материал
                </th>
                <th className="text-left px-4 py-3 text-white/40 font-medium">
                  НК
                </th>
                <th className="text-left px-4 py-3 text-white/40 font-medium">
                  Статус
                </th>
                <th className="text-left px-4 py-3 text-white/40 font-medium">
                  Создан
                </th>
                <th className="text-right px-4 py-3 text-white/40 font-medium">
                  Действия
                </th>
              </tr>
            </thead>
            <tbody>
              {plans.map((plan) => (
                <tr
                  key={plan.id}
                  className="border-b border-white/5 hover:bg-zinc-800/50 transition-colors"
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/technology/${plan.id}`}
                      className="text-white hover:text-blue-300 font-medium transition-colors"
                    >
                      {plan.product_name}
                    </Link>
                    {plan.route_summary && (
                      <p className="text-white/30 text-xs mt-0.5 truncate max-w-xs">
                        {plan.route_summary}
                      </p>
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono text-white/60 text-xs">
                    {plan.product_code || "—"}
                  </td>
                  <td className="px-4 py-3 text-white/50 text-xs">
                    {plan.tp_type || "единичный"}
                  </td>
                  <td className="px-4 py-3 text-white/50 text-xs">
                    {plan.material || "—"}
                  </td>
                  <td className="px-4 py-3">
                    <NcBadge status={plan.normcontrol_status} />
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={clsx(
                        "text-xs font-medium",
                        STATUS_COLORS[plan.status] || "text-white/40",
                      )}
                    >
                      {STATUS_LABELS[plan.status] || plan.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-white/40 text-xs">
                    {new Date(plan.created_at).toLocaleDateString("ru")}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <Link
                        href={`/technology/${plan.id}/review`}
                        className="text-xs text-white/30 hover:text-blue-400 transition-colors"
                        title="Открыть редактор ТП"
                      >
                        Ред.
                      </Link>
                      <a
                        href={`/api/technology/process-plans/${plan.id}/export?format=excel`}
                        className="text-xs text-white/30 hover:text-emerald-400 transition-colors"
                        title="Скачать Excel"
                      >
                        XLS
                      </a>
                      <Link
                        href={`/technology/${plan.id}`}
                        className="text-xs text-white/30 hover:text-white transition-colors"
                      >
                        →
                      </Link>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3 mt-5">
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page === 1}
            className="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 text-white/70 rounded text-sm"
          >
            ←
          </button>
          <span className="text-white/50 text-sm">
            {page} / {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-30 text-white/70 rounded text-sm"
          >
            →
          </button>
        </div>
      )}
    </div>
  );
}
