"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useEffect, useState } from "react";
import { mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

interface AutoApprovalRule {
  id: string;
  name: string;
  is_active: boolean;
  supplier_id: string | null;
  doc_type: string | null;
  max_amount: number | null;
  currency: string | null;
  min_trust_score: number | null;
  approval_role: string;
  apply_count: number;
  last_applied_at: string | null;
  created_at: string;
}

const DOC_TYPE_OPTIONS = [
  { value: "", label: "Любой тип" },
  { value: "invoice", label: "Счёт" },
  { value: "contract", label: "Договор" },
  { value: "act", label: "Акт" },
  { value: "waybill", label: "Накладная" },
];

const EMPTY_FORM = {
  name: "",
  supplier_id: "",
  doc_type: "",
  max_amount: "",
  currency: "RUB",
  min_trust_score: "",
  approval_role: "auto",
};

export default function AutoApprovalSettingsPage() {
  const [rules, setRules] = useState<AutoApprovalRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [creating, setCreating] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  }

  async function load() {
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/auto-approval-rules`);
      if (res.ok) setRules(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function create() {
    if (!form.name.trim()) return;
    setCreating(true);
    try {
      const body: Record<string, unknown> = { name: form.name.trim() };
      if (form.supplier_id) body.supplier_id = form.supplier_id;
      if (form.doc_type) body.doc_type = form.doc_type;
      if (form.max_amount) body.max_amount = parseFloat(form.max_amount);
      if (form.currency) body.currency = form.currency;
      if (form.min_trust_score)
        body.min_trust_score = parseFloat(form.min_trust_score) / 100;
      body.approval_role = form.approval_role || "auto";

      const res = await mutFetch(`${API}/api/auto-approval-rules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.ok) {
        showToast("Правило создано");
        setShowCreate(false);
        setForm(EMPTY_FORM);
        await load();
      }
    } finally {
      setCreating(false);
    }
  }

  async function toggleActive(rule: AutoApprovalRule) {
    const res = await mutFetch(`${API}/api/auto-approval-rules/${rule.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active: !rule.is_active }),
    });
    if (res.ok) {
      setRules((prev) =>
        prev.map((r) =>
          r.id === rule.id ? { ...r, is_active: !r.is_active } : r,
        ),
      );
      showToast(rule.is_active ? "Правило отключено" : "Правило активировано");
    }
  }

  async function deleteRule(id: string) {
    const res = await mutFetch(`${API}/api/auto-approval-rules/${id}`, {
      method: "DELETE",
    });
    if (res.status === 204) {
      setRules((prev) => prev.filter((r) => r.id !== id));
      showToast("Правило удалено");
    }
  }

  return (
    <div className="max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-xl font-bold">Правила авто-утверждения</h1>
          <p className="text-xs text-muted-foreground mt-0.5">
            Счета, соответствующие условиям, утверждаются автоматически без
            ручной проверки
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600"
        >
          + Новое правило
        </button>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="bg-white border border-slate-200 rounded-lg p-4 mb-5 space-y-3">
          <h3 className="text-sm font-semibold">
            Новое правило авто-утверждения
          </h3>
          <p className="text-xs text-slate-500">
            Все заданные условия должны выполняться одновременно. Оставьте поле
            пустым, чтобы не ограничивать по этому критерию.
          </p>
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2">
              <label className="text-xs text-slate-500 block mb-1">
                Название правила *
              </label>
              <input
                autoFocus
                value={form.name}
                onChange={(e) =>
                  setForm((f) => ({ ...f, name: e.target.value }))
                }
                placeholder="Напр.: Мелкие закупки до 50к от доверенных поставщиков"
                className="w-full text-sm border rounded px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Тип документа
              </label>
              <select
                value={form.doc_type}
                onChange={(e) =>
                  setForm((f) => ({ ...f, doc_type: e.target.value }))
                }
                className="w-full text-sm border rounded px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
              >
                {DOC_TYPE_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                ID поставщика (необязательно)
              </label>
              <input
                value={form.supplier_id}
                onChange={(e) =>
                  setForm((f) => ({ ...f, supplier_id: e.target.value }))
                }
                placeholder="UUID поставщика"
                className="w-full text-sm border rounded px-3 py-1.5 font-mono focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Макс. сумма
              </label>
              <div className="flex gap-2">
                <input
                  type="number"
                  value={form.max_amount}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, max_amount: e.target.value }))
                  }
                  placeholder="50000"
                  className="flex-1 text-sm border rounded px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
                />
                <select
                  value={form.currency}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, currency: e.target.value }))
                  }
                  className="w-20 text-sm border rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
                >
                  <option>RUB</option>
                  <option>USD</option>
                  <option>EUR</option>
                </select>
              </div>
            </div>
            <div>
              <label className="text-xs text-slate-500 block mb-1">
                Мин. Trust Score (%)
              </label>
              <input
                type="number"
                min={0}
                max={100}
                value={form.min_trust_score}
                onChange={(e) =>
                  setForm((f) => ({ ...f, min_trust_score: e.target.value }))
                }
                placeholder="80"
                className="w-full text-sm border rounded px-3 py-1.5 focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
            </div>
          </div>
          <div className="flex gap-2 justify-end mt-2">
            <button
              onClick={() => {
                setShowCreate(false);
                setForm(EMPTY_FORM);
              }}
              className="px-3 py-1.5 text-sm border rounded-md hover:bg-slate-50"
            >
              Отмена
            </button>
            <button
              onClick={create}
              disabled={creating || !form.name.trim()}
              className="px-4 py-1.5 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600 disabled:opacity-50"
            >
              {creating ? "Создаю..." : "Создать"}
            </button>
          </div>
        </div>
      )}

      {/* Rules list */}
      {loading ? (
        <div className="py-8 text-center text-slate-400 text-sm">
          Загрузка...
        </div>
      ) : rules.length === 0 ? (
        <div className="py-16 text-center">
          <div className="text-3xl text-slate-300 mb-3">✓</div>
          <p className="text-slate-500 text-sm">Правил авто-утверждения нет.</p>
          <p className="text-slate-400 text-xs mt-1">
            Добавьте правило, чтобы небольшие счета от надёжных поставщиков
            проходили без ручного просмотра.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {rules.map((rule) => (
            <div
              key={rule.id}
              className={`bg-white border rounded-lg p-4 transition-opacity ${rule.is_active ? "border-slate-200" : "border-slate-100 opacity-60"}`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-medium text-slate-800">
                      {rule.name}
                    </span>
                    <span
                      className={`text-[10px] px-2 py-0.5 rounded-full ${rule.is_active ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-500"}`}
                    >
                      {rule.is_active ? "Активно" : "Отключено"}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-slate-500">
                    {rule.doc_type && <span>Тип: {rule.doc_type}</span>}
                    {rule.max_amount != null && (
                      <span>
                        Сумма ≤{" "}
                        {rule.max_amount.toLocaleString("ru-RU", {
                          maximumFractionDigits: 0,
                        })}{" "}
                        {rule.currency ?? ""}
                      </span>
                    )}
                    {rule.min_trust_score != null && (
                      <span>
                        Trust Score ≥ {(rule.min_trust_score * 100).toFixed(0)}%
                      </span>
                    )}
                    {rule.supplier_id && (
                      <span className="font-mono">
                        Поставщик: {rule.supplier_id.slice(0, 8)}…
                      </span>
                    )}
                  </div>
                  <div className="mt-1.5 text-xs text-slate-400">
                    Применено раз: {rule.apply_count}
                    {rule.last_applied_at && (
                      <span className="ml-3">
                        Последний раз:{" "}
                        {new Date(rule.last_applied_at).toLocaleString("ru-RU")}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex gap-1.5 shrink-0">
                  <button
                    onClick={() => toggleActive(rule)}
                    className={`px-2.5 py-1 text-xs rounded border transition-colors ${rule.is_active ? "border-slate-200 text-slate-600 hover:bg-slate-50" : "border-green-200 text-green-700 hover:bg-green-50"}`}
                  >
                    {rule.is_active ? "Отключить" : "Включить"}
                  </button>
                  <button
                    onClick={() => deleteRule(rule.id)}
                    className="px-2 py-1 text-xs text-slate-400 hover:text-red-500 border border-transparent rounded hover:border-red-200"
                  >
                    ✕
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {toast && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 bg-slate-800 text-white text-sm rounded-lg shadow-lg z-50">
          {toast}
        </div>
      )}
    </div>
  );
}
