"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useState } from "react";

const API = getApiBaseUrl();

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

  const fetchAnomalies = () => {
    const params = new URLSearchParams();
    if (statusFilter) params.set("status", statusFilter);
    if (severityFilter) params.set("severity", severityFilter);

    fetch(`${API}/api/anomalies?${params}`)
      .then((r) => r.json())
      .then(setAnomalies)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchAnomalies();
  }, [statusFilter, severityFilter]);

  const handleResolve = async (id: string, resolution: string) => {
    await fetch(`${API}/api/anomalies/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolution }),
    });
    fetchAnomalies();
  };

  if (loading) return <div className="p-6 text-slate-400">Загрузка...</div>;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <h1 className="text-xl font-bold mb-4">Аномалии</h1>

      {/* Filters */}
      <div className="flex gap-3 mb-4">
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

      {anomalies.length === 0 ? (
        <div className="text-slate-400 text-sm">Аномалий не найдено</div>
      ) : (
        <div className="space-y-3">
          {anomalies.map((a) => (
            <div
              key={a.id}
              className={`border rounded-lg p-4 ${SEVERITY_STYLES[a.severity] ?? "bg-slate-800 border-slate-700"}`}
            >
              <div className="flex items-start justify-between">
                <div>
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
                {a.status === "open" && (
                  <div className="flex gap-2 shrink-0 ml-4">
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
      )}
    </div>
  );
}
