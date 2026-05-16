"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useCallback, useEffect, useRef, useState } from "react";

const API = getApiBaseUrl();
const PAGE_SIZE = 50;

interface Anomaly {
  id: string;
  anomaly_type: string;
  severity: string;
  status: string;
  entity_type: string;
  entity_id: string;
  title: string;
  description: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
}

const SEVERITY_STYLES: Record<string, string> = {
  critical: "bg-red-950/40 text-red-300 border-red-700/40",
  warning: "bg-amber-950/40 text-amber-300 border-amber-700/40",
  info: "bg-blue-950/40 text-blue-300 border-blue-700/40",
};

const STATUS_LABELS: Record<string, string> = {
  open: "Открыта",
  resolved: "Решена",
  false_positive: "Ложная",
};

const TYPE_LABELS: Record<string, string> = {
  duplicate: "Дубликат",
  new_supplier: "Новый поставщик",
  requisite_change: "Смена реквизитов",
  price_spike: "Скачок цены",
  unknown_item: "Неизвестная позиция",
  invoice_email_mismatch: "Расхождение с письмом",
};

export default function AnomaliesPage() {
  const [anomalies, setAnomalies] = useState<Anomaly[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [severityFilter, setSeverityFilter] = useState<string>("");
  const [query, setQuery] = useState<string>("");
  const [debouncedQuery, setDebouncedQuery] = useState<string>("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkLoading, setBulkLoading] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounce search query
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setDebouncedQuery(query), 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query]);

  // Reset offset when filters change
  useEffect(() => {
    setOffset(0);
    setSelected(new Set());
  }, [statusFilter, severityFilter, debouncedQuery]);

  const fetchAnomalies = useCallback(() => {
    const params = new URLSearchParams();
    if (statusFilter) params.set("status", statusFilter);
    if (severityFilter) params.set("severity", severityFilter);
    if (debouncedQuery) params.set("q", debouncedQuery);
    params.set("limit", String(PAGE_SIZE + 1));
    params.set("offset", String(offset));

    setLoading(true);
    fetch(`${API}/api/anomalies?${params}`, { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        const items: Anomaly[] = data.items ?? data ?? [];
        setHasMore(items.length > PAGE_SIZE);
        setAnomalies(items.slice(0, PAGE_SIZE));
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [statusFilter, severityFilter, debouncedQuery, offset]);

  useEffect(() => {
    fetchAnomalies();
  }, [fetchAnomalies]);

  const handleResolve = async (id: string, resolution: string) => {
    await fetch(`${API}/api/anomalies/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ resolution }),
      credentials: "include",
    });
    fetchAnomalies();
  };

  const handleBulkResolve = async (resolution: string) => {
    if (selected.size === 0) return;
    setBulkLoading(true);
    try {
      await fetch(`${API}/api/anomalies/bulk-resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ ids: Array.from(selected), resolution }),
        credentials: "include",
      });
      setSelected(new Set());
      fetchAnomalies();
    } finally {
      setBulkLoading(false);
    }
  };

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    const openIds = anomalies
      .filter((a) => a.status === "open")
      .map((a) => a.id);
    if (openIds.every((id) => selected.has(id))) {
      setSelected(new Set());
    } else {
      setSelected(new Set(openIds));
    }
  };

  const openAnomalies = anomalies.filter((a) => a.status === "open");
  const allOpen =
    openAnomalies.length > 0 && openAnomalies.every((a) => selected.has(a.id));

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <h1 className="text-xl font-bold mb-4">Аномалии</h1>

      {/* Filters + Search */}
      <div className="flex flex-wrap gap-3 mb-4">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Поиск по названию или описанию..."
          className="bg-slate-800 border border-slate-600 text-slate-200 rounded px-3 py-1 text-sm min-w-[220px] flex-1"
        />
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="bg-slate-800 border border-slate-600 text-slate-200 rounded px-2 py-1 text-sm"
        >
          <option value="">Все статусы</option>
          <option value="open">Открытые</option>
          <option value="resolved">Решённые</option>
          <option value="false_positive">Ложные</option>
        </select>
        <select
          value={severityFilter}
          onChange={(e) => setSeverityFilter(e.target.value)}
          className="bg-slate-800 border border-slate-600 text-slate-200 rounded px-2 py-1 text-sm"
        >
          <option value="">Все уровни</option>
          <option value="critical">Критические</option>
          <option value="warning">Предупреждения</option>
          <option value="info">Информация</option>
        </select>
      </div>

      {/* Bulk actions bar */}
      {selected.size > 0 && (
        <div className="flex items-center gap-3 mb-4 px-3 py-2 bg-slate-800 border border-slate-600 rounded-lg text-sm">
          <span className="text-slate-300">Выбрано: {selected.size}</span>
          <button
            onClick={() => handleBulkResolve("resolved")}
            disabled={bulkLoading}
            className="px-3 py-1 bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50"
          >
            Решить все
          </button>
          <button
            onClick={() => handleBulkResolve("false_positive")}
            disabled={bulkLoading}
            className="px-3 py-1 bg-slate-600 text-white rounded hover:bg-slate-500 disabled:opacity-50"
          >
            Ложные
          </button>
          <button
            onClick={() => setSelected(new Set())}
            className="ml-auto text-slate-400 hover:text-slate-200"
          >
            Отмена
          </button>
        </div>
      )}

      {loading ? (
        <div className="text-slate-400 text-sm">Загрузка...</div>
      ) : anomalies.length === 0 ? (
        <div className="text-slate-400 text-sm">Аномалий не найдено</div>
      ) : (
        <>
          {/* Select all header */}
          {openAnomalies.length > 0 && (
            <div className="flex items-center gap-2 mb-2 px-1">
              <input
                type="checkbox"
                checked={allOpen}
                onChange={toggleSelectAll}
                className="rounded"
              />
              <span className="text-xs text-slate-400">
                Выбрать все открытые ({openAnomalies.length})
              </span>
            </div>
          )}

          <div className="space-y-3">
            {anomalies.map((a) => (
              <div
                key={a.id}
                className={`border rounded-lg p-4 ${SEVERITY_STYLES[a.severity] ?? "bg-slate-800 border-slate-700"}`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-3 flex-1 min-w-0">
                    {a.status === "open" && (
                      <input
                        type="checkbox"
                        checked={selected.has(a.id)}
                        onChange={() => toggleSelect(a.id)}
                        className="mt-1 rounded shrink-0"
                      />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-medium uppercase">
                          {TYPE_LABELS[a.anomaly_type] ?? a.anomaly_type}
                        </span>
                        <span
                          className={`text-xs px-1.5 py-0.5 rounded ${
                            a.status === "open"
                              ? "bg-white/10"
                              : "bg-green-900/40 text-green-400"
                          }`}
                        >
                          {STATUS_LABELS[a.status] ?? a.status}
                        </span>
                      </div>
                      <div className="font-medium text-sm">{a.title}</div>
                      {a.description && (
                        <div className="text-xs mt-1 opacity-75">
                          {a.description}
                        </div>
                      )}
                      <div className="text-xs mt-1 opacity-50">
                        {new Date(a.created_at).toLocaleString("ru-RU")}
                      </div>
                    </div>
                  </div>
                  {a.status === "open" && (
                    <div className="flex gap-2 shrink-0">
                      <button
                        onClick={() => handleResolve(a.id, "resolved")}
                        className="px-3 py-1 text-xs bg-green-600 text-white rounded hover:bg-green-700"
                      >
                        Решить
                      </button>
                      <button
                        onClick={() => handleResolve(a.id, "false_positive")}
                        className="px-3 py-1 text-xs bg-slate-600 text-white rounded hover:bg-slate-700"
                      >
                        Ложная
                      </button>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* Pagination */}
          <div className="flex items-center justify-between mt-4 text-sm text-slate-400">
            <button
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
              className="px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
            >
              ← Назад
            </button>
            <span>
              {offset + 1}–{offset + anomalies.length}
            </span>
            <button
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={!hasMore}
              className="px-3 py-1 rounded bg-slate-800 hover:bg-slate-700 disabled:opacity-40"
            >
              Вперёд →
            </button>
          </div>
        </>
      )}
    </div>
  );
}
