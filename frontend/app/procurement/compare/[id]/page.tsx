"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

interface AlignedItem {
  canonical_name: string;
  items: Record<
    string,
    {
      description: string;
      qty: number;
      unit_price: number;
      amount: number;
    } | null
  >;
}

interface CompareSession {
  id: string;
  name: string;
  status: string;
  invoice_ids: string[];
  alignment: { items: AlignedItem[] } | null;
  decision: { chosen_supplier_id: string; reasoning: string } | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

interface SupplierSummary {
  supplier_id: string;
  supplier_name: string;
  total: number;
  item_count: number;
}

interface CompareSummary {
  session_id: string;
  total_items: number;
  suppliers: SupplierSummary[];
  cheapest_total: {
    supplier_id: string;
    supplier_name: string;
    total: number;
  } | null;
  recommendation: string | null;
}

function fmt(n: number | null | undefined) {
  if (n == null) return "—";
  return n.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}

function PriceBar({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  const isCheapest =
    pct === (100 * Math.min(value)) / max || value === Math.min(value, max);
  return (
    <div className="flex items-center gap-1.5 mt-0.5">
      <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full ${value === max ? "bg-red-500" : "bg-green-500"}`}
          style={{ width: `${pct.toFixed(1)}%` }}
        />
      </div>
      <span className="text-[10px] text-slate-400 w-16 text-right">
        {fmt(value)}
      </span>
    </div>
  );
}

export default function CompareSessionPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const [session, setSession] = useState<CompareSession | null>(null);
  const [summary, setSummary] = useState<CompareSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [aligning, setAligning] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [chosenSupplier, setChosenSupplier] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sessRes, sumRes] = await Promise.all([
        mutFetch(`${API}/api/compare/${id}`),
        mutFetch(`${API}/api/compare/${id}/summary`),
      ]);
      if (sessRes.ok) setSession(await sessRes.json());
      if (sumRes.ok) setSummary(await sumRes.json());
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function runAlign() {
    setAligning(true);
    setError(null);
    try {
      const res = await mutFetch(`${API}/api/compare/${id}/align`, {
        method: "POST",
      });
      if (res.ok) {
        await load();
      } else {
        const err = await res.json().catch(() => ({}));
        setError(err.detail ?? "Ошибка выравнивания");
      }
    } finally {
      setAligning(false);
    }
  }

  async function submitDecision() {
    if (!chosenSupplier) return;
    setDeciding(true);
    setError(null);
    try {
      const res = await mutFetch(`${API}/api/compare/${id}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chosen_supplier_id: chosenSupplier,
          reasoning: reasoning || null,
        }),
      });
      if (res.ok) {
        const updated: CompareSession = await res.json();
        setSession(updated);
      } else {
        const err = await res.json().catch(() => ({}));
        setError(err.detail ?? "Ошибка принятия решения");
      }
    } finally {
      setDeciding(false);
    }
  }

  if (loading)
    return <div className="p-6 text-slate-400 text-sm">Загрузка...</div>;
  if (!session)
    return <div className="p-6 text-slate-400 text-sm">Сессия не найдена</div>;

  const alignedItems = session.alignment?.items ?? [];
  const supplierIds = summary?.suppliers.map((s) => s.supplier_id) ?? [
    ...new Set(alignedItems.flatMap((it) => Object.keys(it.items))),
  ];

  const maxTotals = summary
    ? Math.max(...summary.suppliers.map((s) => s.total))
    : 0;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <button
        onClick={() => router.push("/procurement")}
        className="text-sm text-slate-500 hover:text-slate-300 mb-4 block"
      >
        ← Закупки
      </button>

      {/* Header */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-slate-100">{session.name}</h1>
          <p className="text-xs text-slate-500 mt-1">
            {session.invoice_ids.length} КП · создана{" "}
            {new Date(session.created_at).toLocaleDateString("ru-RU")}
          </p>
        </div>
        <div className="flex gap-2 items-center">
          <span
            className={`text-xs px-2 py-0.5 rounded-full ${
              session.status === "decided"
                ? "bg-green-900/40 text-green-400"
                : session.status === "aligned"
                  ? "bg-blue-900/40 text-blue-400"
                  : "bg-slate-700 text-slate-400"
            }`}
          >
            {session.status}
          </span>
          {session.status === "pending" && (
            <button
              onClick={runAlign}
              disabled={aligning}
              className="px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {aligning ? "Выравниваю…" : "Выровнять позиции"}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="mb-4 px-4 py-2 bg-red-500/10 border border-red-500/30 rounded text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Summary cards */}
      {summary && summary.suppliers.length > 0 && (
        <div
          className="grid gap-3 mb-6"
          style={{
            gridTemplateColumns: `repeat(${Math.min(summary.suppliers.length, 4)}, minmax(0, 1fr))`,
          }}
        >
          {summary.suppliers.map((s) => {
            const isCheapest =
              summary.cheapest_total?.supplier_id === s.supplier_id;
            return (
              <div
                key={s.supplier_id}
                className={`bg-slate-800 border rounded-lg p-4 ${isCheapest ? "border-green-600/60" : "border-slate-700"}`}
              >
                <div className="flex items-start justify-between">
                  <div className="text-sm font-semibold text-slate-200 truncate flex-1">
                    {s.supplier_name}
                  </div>
                  {isCheapest && (
                    <span className="text-[10px] px-1.5 py-0.5 bg-green-900/40 text-green-400 rounded-full shrink-0 ml-1">
                      лучший
                    </span>
                  )}
                </div>
                <div className="text-xl font-bold text-slate-100 mt-2">
                  {fmt(s.total)}
                </div>
                <div className="text-xs text-slate-500">
                  {s.item_count} позиций
                </div>
                <PriceBar value={s.total} max={maxTotals} />
              </div>
            );
          })}
        </div>
      )}

      {/* AI recommendation */}
      {summary?.recommendation && (
        <div className="mb-5 px-4 py-3 bg-purple-900/20 border border-purple-700/40 rounded-lg">
          <p className="text-xs font-semibold text-purple-400 mb-1">
            AI-рекомендация
          </p>
          <p className="text-sm text-slate-300">{summary.recommendation}</p>
        </div>
      )}

      {/* Alignment table */}
      {alignedItems.length > 0 && (
        <div className="mb-6">
          <h2 className="text-sm font-semibold text-slate-300 mb-3">
            Сравнение позиций
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="bg-slate-800">
                  <th className="text-left px-3 py-2 text-xs text-slate-400 border-b border-slate-700 font-medium">
                    Позиция
                  </th>
                  {supplierIds.map((sid) => {
                    const sup = summary?.suppliers.find(
                      (s) => s.supplier_id === sid,
                    );
                    return (
                      <th
                        key={sid}
                        className="text-right px-3 py-2 text-xs text-slate-400 border-b border-slate-700 font-medium"
                      >
                        {sup?.supplier_name ?? sid.slice(0, 8)}
                      </th>
                    );
                  })}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {alignedItems.map((item) => {
                  const prices = supplierIds
                    .map((sid) => item.items[sid]?.unit_price ?? null)
                    .filter((p) => p != null) as number[];
                  const minPrice =
                    prices.length > 0 ? Math.min(...prices) : null;
                  const maxPrice =
                    prices.length > 0 ? Math.max(...prices) : null;
                  return (
                    <tr
                      key={item.canonical_name}
                      className="hover:bg-slate-800/40"
                    >
                      <td className="px-3 py-2 text-slate-300 font-medium max-w-xs">
                        {item.canonical_name}
                      </td>
                      {supplierIds.map((sid) => {
                        const entry = item.items[sid];
                        const price = entry?.unit_price ?? null;
                        const isBest =
                          price != null &&
                          price === minPrice &&
                          maxPrice !== minPrice;
                        const isWorst =
                          price != null &&
                          price === maxPrice &&
                          maxPrice !== minPrice;
                        return (
                          <td
                            key={sid}
                            className={`px-3 py-2 text-right font-mono text-xs ${
                              isBest
                                ? "text-green-400 font-semibold"
                                : isWorst
                                  ? "text-red-400"
                                  : "text-slate-300"
                            }`}
                          >
                            {entry ? (
                              <span title={entry.description}>
                                {fmt(entry.unit_price)}
                                <span className="text-slate-600 ml-1 text-[10px]">
                                  ×{entry.qty}
                                </span>
                              </span>
                            ) : (
                              <span className="text-slate-700">—</span>
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Decision */}
      {session.status === "decided" && session.decision ? (
        <div className="bg-green-900/20 border border-green-700/40 rounded-lg p-4">
          <p className="text-sm font-semibold text-green-400 mb-1">
            Решение принято
          </p>
          <p className="text-xs text-slate-400">
            Поставщик:{" "}
            <span className="text-slate-200">
              {session.decision.chosen_supplier_id}
            </span>
          </p>
          {session.decision.reasoning && (
            <p className="text-xs text-slate-400 mt-1">
              {session.decision.reasoning}
            </p>
          )}
          {session.decided_at && (
            <p className="text-xs text-slate-600 mt-1">
              {new Date(session.decided_at).toLocaleString("ru-RU")}
            </p>
          )}
        </div>
      ) : session.status === "aligned" ? (
        <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
          <h3 className="text-sm font-semibold text-slate-200 mb-3">
            Принять решение
          </h3>
          <div className="flex gap-2 flex-wrap items-end">
            <div className="flex-1 min-w-48">
              <label className="text-xs text-slate-400 block mb-1">
                Выбранный поставщик
              </label>
              <select
                value={chosenSupplier}
                onChange={(e) => setChosenSupplier(e.target.value)}
                className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 rounded outline-none focus:border-blue-400"
              >
                <option value="">— выберите —</option>
                {(summary?.suppliers ?? []).map((s) => (
                  <option key={s.supplier_id} value={s.supplier_id}>
                    {s.supplier_name} ({fmt(s.total)})
                  </option>
                ))}
              </select>
            </div>
            <div className="flex-1 min-w-48">
              <label className="text-xs text-slate-400 block mb-1">
                Обоснование
              </label>
              <input
                type="text"
                value={reasoning}
                onChange={(e) => setReasoning(e.target.value)}
                placeholder="Причина выбора..."
                className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
              />
            </div>
            <button
              onClick={submitDecision}
              disabled={deciding || !chosenSupplier}
              className="px-4 py-1.5 text-sm bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50"
            >
              {deciding ? "Сохраняю…" : "Утвердить"}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
