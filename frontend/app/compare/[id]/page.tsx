"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

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

interface CompareSession {
  id: string;
  name: string;
  status: string;
  invoice_ids: string[];
  alignment: {
    items: AlignedItem[];
    suppliers: Record<string, SupplierInfo>;
  } | null;
  decision: {
    chosen_supplier_id: string;
    reasoning: string | null;
  } | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

interface SummarySupplier {
  supplier_id: string;
  name: string;
  invoice_number: string | null;
  total: number;
  invoice_total: number | null;
}

interface Summary {
  total_items: number;
  suppliers: SummarySupplier[];
  cheapest_total: SummarySupplier | null;
  recommendation: string | null;
}

export default function CompareDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [session, setSession] = useState<CompareSession | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [aligning, setAligning] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [chosenId, setChosenId] = useState("");
  const [reasoning, setReasoning] = useState("");
  const [showDecideForm, setShowDecideForm] = useState(false);
  const [loading, setLoading] = useState(true);
  const [creatingDraft, setCreatingDraft] = useState(false);

  async function loadSession() {
    const res = await mutFetch(`${API}/api/compare/${id}`);
    if (!res.ok) {
      router.push("/compare");
      return;
    }
    const data: CompareSession = await res.json();
    setSession(data);

    if (data.status !== "draft") {
      const sumRes = await mutFetch(`${API}/api/compare/${id}/summary`);
      if (sumRes.ok) setSummary(await sumRes.json());
    }
    setLoading(false);
  }

  useEffect(() => {
    loadSession();
  }, [id]);

  async function align() {
    setAligning(true);
    try {
      const res = await mutFetch(`${API}/api/compare/${id}/align`, {
        method: "POST",
      });
      if (res.ok) await loadSession();
    } finally {
      setAligning(false);
    }
  }

  async function decide() {
    if (!chosenId) return;
    setDeciding(true);
    try {
      const res = await mutFetch(`${API}/api/compare/${id}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chosen_supplier_id: chosenId,
          reasoning: reasoning || null,
        }),
      });
      if (res.ok) {
        setShowDecideForm(false);
        await loadSession();
      }
    } finally {
      setDeciding(false);
    }
  }

  async function createDecisionDraft() {
    if (!session?.decision) return;
    setCreatingDraft(true);
    try {
      const chosenSupplier =
        suppliers[session.decision.chosen_supplier_id]?.name ??
        session.decision.chosen_supplier_id;
      const res = await mutFetch(`${API}/api/drafts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          subject: `Решение по сравнению КП: ${session.name}`,
          body_text:
            `Уважаемые коллеги,\n\n` +
            `По результатам сравнения коммерческих предложений «${session.name}» ` +
            `выбран поставщик: ${chosenSupplier}.\n\n` +
            (session.decision.reasoning
              ? `Обоснование: ${session.decision.reasoning}\n\n`
              : "") +
            `С уважением,\nСистема документооборота`,
          to_addresses: [],
          related_entity_type: "compare_session",
          related_entity_id: session.id,
        }),
      });
      if (res.ok) {
        const draft = await res.json();
        router.push(`/email?draft=${draft.id}`);
      }
    } finally {
      setCreatingDraft(false);
    }
  }

  if (loading) {
    return (
      <div className="p-6 text-center text-slate-500 text-sm">Загрузка...</div>
    );
  }
  if (!session) return null;

  const suppliers = session.alignment?.suppliers ?? {};
  const supplierIds = Object.keys(suppliers);
  const items = session.alignment?.items ?? [];

  return (
    <div className="p-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <button
            onClick={() => router.push("/compare")}
            className="text-xs text-slate-500 hover:text-slate-300 mb-1"
          >
            ← Все сравнения
          </button>
          <h1 className="text-xl font-bold text-slate-100">{session.name}</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            {session.invoice_ids.length} предложений ·{" "}
            {new Date(session.created_at).toLocaleDateString("ru-RU")}
          </p>
        </div>
        <div className="flex gap-2">
          {session.status === "draft" && (
            <button
              onClick={align}
              disabled={aligning}
              className="px-4 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {aligning ? "Выравниваю..." : "Выровнять позиции"}
            </button>
          )}
          {session.status === "aligned" && !showDecideForm && (
            <button
              onClick={() => setShowDecideForm(true)}
              className="px-4 py-1.5 text-sm bg-green-600 text-white rounded hover:bg-green-700"
            >
              Принять решение
            </button>
          )}
        </div>
      </div>

      {/* Summary banner */}
      {summary && summary.recommendation && (
        <div className="mb-4 px-4 py-3 bg-blue-950/30 border border-blue-700/40 rounded-lg">
          <p className="text-sm text-blue-300">{summary.recommendation}</p>
        </div>
      )}

      {/* Decision decided */}
      {session.status === "decided" && session.decision && (
        <div className="mb-4 px-4 py-3 bg-green-950/30 border border-green-700/40 rounded-lg flex items-start justify-between gap-3">
          <div>
            <p className="text-sm font-medium text-green-300">
              ✓ Выбрано:{" "}
              {suppliers[session.decision.chosen_supplier_id]?.name ??
                session.decision.chosen_supplier_id}
            </p>
            {session.decision.reasoning && (
              <p className="text-xs text-green-500 mt-0.5">
                {session.decision.reasoning}
              </p>
            )}
          </div>
          <button
            onClick={createDecisionDraft}
            disabled={creatingDraft}
            className="shrink-0 px-3 py-1.5 text-xs bg-slate-700 text-slate-300 rounded hover:bg-slate-600 disabled:opacity-50"
          >
            {creatingDraft ? "Создаю..." : "✉ Уведомить"}
          </button>
        </div>
      )}

      {/* Decide form */}
      {showDecideForm && session.status === "aligned" && (
        <div className="mb-5 bg-slate-800 border border-slate-700 rounded-lg p-4 space-y-3">
          <h3 className="text-sm font-semibold text-slate-200">
            Принять решение
          </h3>
          <label
            htmlFor="decide-supplier-select"
            className="block text-xs text-slate-400 mb-1"
          >
            Поставщик
          </label>
          <select
            id="decide-supplier-select"
            value={chosenId}
            onChange={(e) => setChosenId(e.target.value)}
            className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 rounded outline-none focus:border-blue-400"
          >
            <option value="">— Выберите поставщика —</option>
            {supplierIds.map((sid) => (
              <option key={sid} value={sid}>
                {suppliers[sid].name}
                {summary?.suppliers.find((s) => s.supplier_id === sid)?.total
                  ? ` — ${summary.suppliers
                      .find((s) => s.supplier_id === sid)!
                      .total.toLocaleString("ru-RU")} ₽`
                  : ""}
              </option>
            ))}
          </select>
          <label
            htmlFor="decide-reasoning"
            className="block text-xs text-slate-400 mb-1"
          >
            Обоснование
          </label>
          <textarea
            id="decide-reasoning"
            value={reasoning}
            onChange={(e) => setReasoning(e.target.value)}
            placeholder="Обоснование (необязательно)"
            rows={2}
            className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400 resize-none"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => setShowDecideForm(false)}
              className="px-3 py-1.5 text-xs text-slate-400"
            >
              Отмена
            </button>
            <button
              onClick={decide}
              disabled={deciding || !chosenId}
              className="px-4 py-1.5 text-xs bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-50"
            >
              {deciding ? "Сохраняю..." : "Подтвердить"}
            </button>
          </div>
        </div>
      )}

      {/* Supplier totals */}
      {summary && summary.suppliers.length > 0 && (
        <div
          className="mb-5 grid gap-3"
          style={{
            gridTemplateColumns: `repeat(${Math.min(summary.suppliers.length, 4)}, 1fr)`,
          }}
        >
          {summary.suppliers.map((sup) => (
            <div
              key={sup.supplier_id}
              className={`px-4 py-3 rounded-lg border ${
                session.decision?.chosen_supplier_id === sup.supplier_id
                  ? "bg-green-950/30 border-green-700/40"
                  : sup.supplier_id === summary.cheapest_total?.supplier_id
                    ? "bg-blue-950/20 border-blue-700/30"
                    : "bg-slate-800 border-slate-700"
              }`}
            >
              <p className="text-xs text-slate-400 truncate">{sup.name}</p>
              <p className="text-lg font-bold text-slate-100 mt-0.5">
                {sup.total.toLocaleString("ru-RU")} ₽
              </p>
              {sup.invoice_number && (
                <p className="text-[10px] text-slate-500">
                  № {sup.invoice_number}
                </p>
              )}
              {sup.supplier_id === summary.cheapest_total?.supplier_id &&
                summary.suppliers.length > 1 && (
                  <span className="text-[10px] text-blue-400">
                    ✓ Минимальная цена
                  </span>
                )}
            </div>
          ))}
        </div>
      )}

      {/* Alignment table */}
      {items.length > 0 ? (
        <div className="bg-slate-800 rounded-lg border border-slate-700 overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700">
                <th className="px-4 py-2.5 text-left text-xs font-medium text-slate-400 w-48">
                  Позиция
                </th>
                {supplierIds.map((sid) => (
                  <th
                    key={sid}
                    className="px-4 py-2.5 text-right text-xs font-medium text-slate-400"
                  >
                    {suppliers[sid]?.name ?? sid.slice(0, 8)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700/50">
              {items.map((item, idx) => {
                // Find min price for highlighting
                const prices = supplierIds
                  .map((sid) => item.items[sid]?.unit_price ?? null)
                  .filter((p): p is number => p !== null);
                const minPrice = prices.length > 0 ? Math.min(...prices) : null;

                return (
                  <tr key={idx} className="hover:bg-slate-700/30">
                    <td className="px-4 py-2.5 text-xs text-slate-300 align-top">
                      {item.canonical_name}
                    </td>
                    {supplierIds.map((sid) => {
                      const d = item.items[sid];
                      if (!d)
                        return (
                          <td
                            key={sid}
                            className="px-4 py-2.5 text-xs text-slate-600 text-right"
                          >
                            —
                          </td>
                        );
                      const isCheapest =
                        minPrice !== null &&
                        d.unit_price === minPrice &&
                        prices.length > 1;
                      return (
                        <td
                          key={sid}
                          className={`px-4 py-2.5 text-right text-xs align-top ${isCheapest ? "text-green-400" : "text-slate-200"}`}
                        >
                          <span className="font-medium">
                            {d.unit_price?.toLocaleString("ru-RU")} ₽
                          </span>
                          <span className="text-slate-500 ml-1">
                            / {d.unit || "шт"}
                          </span>
                          <br />
                          <span className="text-[10px] text-slate-500">
                            {d.quantity} × {d.amount?.toLocaleString("ru-RU")} ₽
                          </span>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : session.status === "draft" ? (
        <div className="py-12 text-center text-slate-500 text-sm">
          <p>Нажмите «Выровнять позиции», чтобы выполнить сравнение.</p>
        </div>
      ) : (
        <div className="py-12 text-center text-slate-500 text-sm">
          Нет позиций для сравнения.
        </div>
      )}
    </div>
  );
}
