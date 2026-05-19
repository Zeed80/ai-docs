"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface CompareSession {
  id: string;
  name: string;
  status: string;
  invoice_ids: string[];
  alignment: {
    items: AlignedItem[];
    suppliers: Record<string, SupplierInfo>;
  } | null;
  decision: { chosen_supplier_id: string; reasoning: string } | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

interface SupplierInfo {
  name: string;
  invoice_number: string | null;
  total_amount: number | null;
}

interface AlignedItem {
  canonical_name: string;
  items: Record<
    string,
    {
      description: string;
      quantity: number;
      unit: string;
      unit_price: number;
      amount: number;
    } | null
  >;
}

interface Invoice {
  id: string;
  invoice_number: string | null;
  supplier_name: string | null;
  total_amount: number | null;
}

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  aligned: "Выровнено",
  decided: "Решение принято",
};

const STATUS_STYLES: Record<string, string> = {
  draft: "bg-slate-700 text-slate-300",
  aligned: "bg-blue-900/40 text-blue-300",
  decided: "bg-green-900/40 text-green-300",
};

export default function ComparePage() {
  const router = useRouter();
  const [sessions, setSessions] = useState<CompareSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [formName, setFormName] = useState("");
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);

  async function loadSessions() {
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/compare`);
      if (res.ok) setSessions(await res.json());
    } finally {
      setLoading(false);
    }
  }

  async function loadInvoices() {
    const res = await fetch(`${API}/api/invoices?limit=100`);
    if (res.ok) {
      const data = await res.json();
      setInvoices(data.items ?? data ?? []);
    }
  }

  useEffect(() => {
    loadSessions();
  }, []);

  useEffect(() => {
    if (showForm) loadInvoices();
  }, [showForm]);

  function toggleInvoice(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function create() {
    if (!formName.trim() || selectedIds.size < 2) return;
    setCreating(true);
    try {
      const res = await fetch(`${API}/api/compare`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: formName.trim(),
          invoice_ids: [...selectedIds],
        }),
      });
      if (res.ok) {
        const session: CompareSession = await res.json();
        setShowForm(false);
        router.push(`/compare/${session.id}`);
      }
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-slate-100">Сравнение КП</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Сравнение коммерческих предложений по позициям
          </p>
        </div>
        <button
          onClick={() => setShowForm(true)}
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          + Новое сравнение
        </button>
      </div>

      {/* Create form */}
      {showForm && (
        <div className="mb-6 bg-slate-800 border border-slate-700 rounded-lg p-4 space-y-4">
          <h3 className="text-sm font-semibold text-slate-200">
            Новое сравнение
          </h3>
          <input
            autoFocus
            type="text"
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            placeholder="Название *"
            className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
          />
          <div>
            <p className="text-xs text-slate-400 mb-2">
              Выберите счета для сравнения (минимум 2):
            </p>
            <div className="max-h-48 overflow-y-auto space-y-1 border border-slate-700 rounded p-2 bg-slate-900/50">
              {invoices.length === 0 ? (
                <p className="text-xs text-slate-500 p-2">
                  Нет доступных счетов
                </p>
              ) : (
                invoices.map((inv) => (
                  <label
                    key={inv.id}
                    className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-slate-700 cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={selectedIds.has(inv.id)}
                      onChange={() => toggleInvoice(inv.id)}
                      className="accent-blue-500"
                    />
                    <span className="text-sm text-slate-200 flex-1 truncate">
                      {inv.invoice_number || inv.id.slice(0, 8)}
                    </span>
                    {inv.supplier_name && (
                      <span className="text-xs text-slate-400 truncate max-w-[120px]">
                        {inv.supplier_name}
                      </span>
                    )}
                    {inv.total_amount != null && (
                      <span className="text-xs text-slate-300 shrink-0">
                        {inv.total_amount.toLocaleString("ru-RU")} ₽
                      </span>
                    )}
                  </label>
                ))
              )}
            </div>
            {selectedIds.size > 0 && (
              <p className="text-xs text-blue-400 mt-1">
                Выбрано: {selectedIds.size} счёт(а)
              </p>
            )}
          </div>
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => {
                setShowForm(false);
                setSelectedIds(new Set());
                setFormName("");
              }}
              className="px-3 py-1.5 text-xs text-slate-400"
            >
              Отмена
            </button>
            <button
              onClick={create}
              disabled={creating || !formName.trim() || selectedIds.size < 2}
              className="px-4 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {creating ? "Создаю..." : "Создать"}
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div className="py-12 text-center text-slate-400 text-sm">
          Загрузка...
        </div>
      ) : sessions.length === 0 ? (
        <div className="py-16 text-center">
          <div className="text-5xl text-slate-700 mb-3">⚖</div>
          <p className="text-slate-400 text-sm">Нет сравнений.</p>
          <p className="text-slate-400 text-xs mt-1">
            Создайте сравнение для двух и более коммерческих предложений.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {sessions.map((s) => (
            <button
              key={s.id}
              onClick={() => router.push(`/compare/${s.id}`)}
              className="w-full text-left flex items-center gap-4 px-4 py-3 bg-slate-800 border border-slate-700 rounded-lg hover:bg-slate-700/60 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-slate-200">{s.name}</p>
                <p className="text-xs text-slate-500 mt-0.5">
                  {s.invoice_ids.length} предложений ·{" "}
                  {new Date(s.created_at).toLocaleDateString("ru-RU")}
                </p>
              </div>
              <span
                className={`text-[10px] px-2 py-0.5 rounded-full ${STATUS_STYLES[s.status] ?? "bg-slate-700 text-slate-400"}`}
              >
                {STATUS_LABELS[s.status] ?? s.status}
              </span>
              {s.decision && (
                <span className="text-xs text-green-400 shrink-0">
                  ✓ Решение принято
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
