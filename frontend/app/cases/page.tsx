"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface WorkCase {
  id: string;
  title: string;
  customer: string | null;
  task_description: string | null;
  status: string;
  created_by: string;
  created_at: string;
  documents_count: number;
}

const STATUS_COLORS: Record<string, string> = {
  open: "bg-blue-900/40 text-blue-300",
  in_progress: "bg-amber-900/40 text-amber-300",
  closed: "bg-slate-700 text-slate-400",
  rejected: "bg-red-900/40 text-red-300",
};

export default function CasesPage() {
  const router = useRouter();
  const [cases, setCases] = useState<WorkCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string | null>(null);

  async function load(status: string | null) {
    setLoading(true);
    try {
      const url = status
        ? `${API}/api/cases?status=${status}`
        : `${API}/api/cases`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        setCases(data.items ?? []);
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(statusFilter);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter]);

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-slate-100">Кейсы</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Рабочие задания — группировка документов, согласований и аудита
          </p>
        </div>
      </div>

      <div className="flex gap-2 mb-4">
        {([null, "open", "in_progress", "closed"] as const).map((s) => (
          <button
            key={String(s)}
            onClick={() => setStatusFilter(s)}
            className={`px-3 py-1 text-xs rounded ${
              statusFilter === s
                ? "bg-slate-600 text-slate-100"
                : "bg-slate-800 text-slate-400 hover:text-slate-200"
            }`}
          >
            {s === null ? "Все" : s}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="py-12 text-center text-slate-500 text-sm">
          Загрузка...
        </div>
      ) : cases.length === 0 ? (
        <div className="py-16 text-center">
          <div className="text-4xl text-slate-700 mb-3">📋</div>
          <p className="text-slate-400 text-sm">Нет кейсов.</p>
          <p className="text-slate-600 text-xs mt-1">
            Создайте кейс на главной странице.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {cases.map((c) => (
            <button
              key={c.id}
              onClick={() => router.push(`/cases/${c.id}`)}
              className="w-full text-left flex items-start gap-4 p-4 bg-slate-800 border border-slate-700 rounded-lg hover:bg-slate-700/60 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <span className="text-sm font-medium text-slate-200 truncate">
                    {c.title}
                  </span>
                </div>
                {c.customer && (
                  <p className="text-xs text-slate-400 truncate">
                    {c.customer}
                  </p>
                )}
                <p className="text-[10px] text-slate-500 mt-1">
                  {c.documents_count} документов ·{" "}
                  {new Date(c.created_at).toLocaleDateString("ru-RU")}
                </p>
              </div>
              <span
                className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 mt-0.5 ${
                  STATUS_COLORS[c.status] ?? "bg-slate-700 text-slate-400"
                }`}
              >
                {c.status}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
