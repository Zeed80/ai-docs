"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";

const API = getApiBaseUrl();

interface BOMLine {
  id: string;
  line_number: number;
  description: string;
  quantity: number;
  unit: string;
  canonical_item_id?: string;
  notes?: string;
}

interface BOM {
  id: string;
  product_name: string;
  product_code?: string;
  version: string;
  status: string;
  approved_by?: string;
  approved_at?: string;
  notes?: string;
  lines: BOMLine[];
  created_at: string;
}

interface StockCheckLine {
  line_number: number;
  description: string;
  required_qty: number;
  unit: string;
  available_qty: number | null;
  shortage: number | null;
}

interface StockCheckResult {
  can_produce: boolean;
  shortage_count: number;
  batch_qty: number;
  lines: StockCheckLine[];
}

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-slate-700 text-slate-300",
  approved: "bg-green-900 text-green-200",
  obsolete: "bg-slate-800 text-slate-500",
};

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  approved: "Утверждена",
  obsolete: "Устарела",
};

function AddLineForm({
  bomId,
  nextLineNumber,
  onAdded,
  onClose,
}: {
  bomId: string;
  nextLineNumber: number;
  onAdded: () => void;
  onClose: () => void;
}) {
  const [description, setDescription] = useState("");
  const [quantity, setQuantity] = useState("1");
  const [unit, setUnit] = useState("шт");
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!description.trim()) {
      setError("Введите наименование");
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/boms/${bomId}/lines`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          line_number: nextLineNumber,
          description,
          quantity: Number(quantity),
          unit,
          notes: notes || null,
        }),
      });
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка");
        return;
      }
      onAdded();
    } catch {
      setError("Ошибка сети");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="bg-slate-800 rounded-lg p-6 w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-slate-100 mb-4">
          Добавить позицию
        </h2>
        {error && <p className="text-red-400 text-sm mb-3">{error}</p>}
        <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Наименование *
            </label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              placeholder="Болт М8×20 ГОСТ 7798-70"
              autoFocus
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Количество *
              </label>
              <input
                type="number"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
                step="0.001"
                min="0"
                className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Единица *
              </label>
              <input
                value={unit}
                onChange={(e) => setUnit(e.target.value)}
                className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              />
            </div>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Примечание
            </label>
            <input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div className="flex gap-3 pt-1">
            <button
              type="submit"
              disabled={loading}
              className="flex-1 py-2 bg-indigo-600 text-white rounded text-sm font-medium hover:bg-indigo-500 disabled:opacity-50"
            >
              {loading ? "Добавление..." : "Добавить"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-slate-400 hover:text-slate-200 text-sm"
            >
              Отмена
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function BOMDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [bom, setBom] = useState<BOM | null>(null);
  const [loading, setLoading] = useState(true);
  const [stockCheck, setStockCheck] = useState<StockCheckResult | null>(null);
  const [stockLoading, setStockLoading] = useState(false);
  const [batchQty, setBatchQty] = useState("1");
  const [showAddLine, setShowAddLine] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  function showToast(msg: string, ms = 3000) {
    setToast(msg);
    setTimeout(() => setToast(null), ms);
  }

  async function loadBom() {
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/boms/${id}`);
      if (!res.ok) {
        router.push("/boms");
        return;
      }
      setBom(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadBom();
  }, [id]);

  async function handleDeleteLine(lineId: string) {
    await fetch(`${API}/api/boms/${id}/lines/${lineId}`, { method: "DELETE" });
    loadBom();
  }

  async function handleApprove() {
    setActionLoading(true);
    try {
      const res = await fetch(
        `${API}/api/boms/${id}/approve?approved_by=user`,
        { method: "POST" },
      );
      if (!res.ok) {
        const d = await res.json();
        showToast(d.detail ?? "Ошибка");
        return;
      }
      showToast("Спецификация утверждена");
      loadBom();
    } finally {
      setActionLoading(false);
    }
  }

  async function handleStockCheck() {
    setStockLoading(true);
    try {
      const res = await fetch(
        `${API}/api/boms/${id}/stock-check?batch_qty=${batchQty}`,
      );
      if (res.ok) setStockCheck(await res.json());
    } finally {
      setStockLoading(false);
    }
  }

  async function handleCreatePR() {
    setActionLoading(true);
    try {
      const res = await fetch(
        `${API}/api/boms/${id}/create-purchase-request?batch_qty=${batchQty}`,
        { method: "POST" },
      );
      if (!res.ok) {
        const d = await res.json();
        showToast(d.detail ?? "Ошибка");
        return;
      }
      const data = await res.json();
      if (data.purchase_request_id) {
        showToast(data.message, 4000);
        router.push(`/procurement`);
      } else {
        showToast(data.message);
      }
    } finally {
      setActionLoading(false);
    }
  }

  if (loading) {
    return <div className="p-6 text-slate-400 text-sm">Загрузка...</div>;
  }
  if (!bom) return null;

  const isDraft = bom.status === "draft";
  const nextLineNumber =
    bom.lines.length > 0
      ? Math.max(...bom.lines.map((l) => l.line_number)) + 1
      : 1;

  return (
    <div className="p-6 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <div className="flex items-center gap-3 mb-1">
            <Link
              href="/boms"
              className="text-slate-500 hover:text-slate-300 text-sm"
            >
              ← Спецификации
            </Link>
          </div>
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold text-slate-100">
              {bom.product_name}
            </h1>
            <span className="text-slate-500 text-sm">v{bom.version}</span>
            {bom.product_code && (
              <span className="text-xs text-slate-500 font-mono bg-slate-800 px-2 py-0.5 rounded">
                {bom.product_code}
              </span>
            )}
            <span
              className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[bom.status] ?? "bg-slate-700 text-slate-300"}`}
            >
              {STATUS_LABELS[bom.status] ?? bom.status}
            </span>
          </div>
          {bom.approved_by && (
            <p className="text-xs text-slate-500 mt-1">
              Утверждена: {bom.approved_by}
              {bom.approved_at &&
                ` — ${new Date(bom.approved_at).toLocaleDateString("ru-RU")}`}
            </p>
          )}
        </div>
        <div className="flex gap-2 shrink-0">
          {isDraft && (
            <button
              onClick={() => setShowAddLine(true)}
              className="px-3 py-1.5 text-sm bg-slate-700 text-slate-200 rounded hover:bg-slate-600"
            >
              + Позиция
            </button>
          )}
          {isDraft && bom.lines.length > 0 && (
            <button
              onClick={handleApprove}
              disabled={actionLoading}
              className="px-3 py-1.5 text-sm bg-green-700 text-white rounded hover:bg-green-600 disabled:opacity-50"
            >
              Утвердить
            </button>
          )}
        </div>
      </div>

      {/* Lines table */}
      <div className="bg-slate-800 rounded-lg overflow-hidden mb-6">
        <div className="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
          <h2 className="text-sm font-medium text-slate-300">
            Состав спецификации ({bom.lines.length} позиций)
          </h2>
        </div>
        {bom.lines.length === 0 ? (
          <div className="text-center py-12 text-slate-500">
            <p className="text-sm">Позиций нет</p>
            {isDraft && (
              <button
                onClick={() => setShowAddLine(true)}
                className="mt-3 px-4 py-2 bg-indigo-600 text-white rounded text-sm hover:bg-indigo-500"
              >
                Добавить позицию
              </button>
            )}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700 text-xs text-slate-500 uppercase tracking-wider">
                <th className="text-left py-2 px-4 w-10">№</th>
                <th className="text-left py-2 pr-4">Наименование</th>
                <th className="text-right py-2 pr-4 w-24">Кол-во</th>
                <th className="text-left py-2 pr-4 w-16">Ед.</th>
                {isDraft && <th className="py-2 px-4 w-12"></th>}
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-700/50">
              {bom.lines.map((line) => (
                <tr key={line.id} className="hover:bg-slate-700/30">
                  <td className="py-2.5 px-4 text-slate-500 text-xs">
                    {line.line_number}
                  </td>
                  <td className="py-2.5 pr-4 text-slate-200">
                    {line.description}
                    {line.notes && (
                      <span className="ml-2 text-xs text-slate-500">
                        {line.notes}
                      </span>
                    )}
                  </td>
                  <td className="py-2.5 pr-4 text-right font-mono text-slate-100">
                    {line.quantity}
                  </td>
                  <td className="py-2.5 pr-4 text-slate-400">{line.unit}</td>
                  {isDraft && (
                    <td className="py-2.5 px-4 text-center">
                      <button
                        onClick={() => handleDeleteLine(line.id)}
                        className="text-slate-600 hover:text-red-400 transition-colors text-xs"
                      >
                        ✕
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Stock Check */}
      {bom.lines.length > 0 && (
        <div className="bg-slate-800 rounded-lg p-4">
          <div className="flex items-center gap-4 mb-4">
            <h2 className="text-sm font-medium text-slate-300">
              Проверка складских остатков
            </h2>
            <div className="flex items-center gap-2">
              <label className="text-xs text-slate-500">Партия:</label>
              <input
                type="number"
                value={batchQty}
                onChange={(e) => setBatchQty(e.target.value)}
                min="0.001"
                step="0.001"
                className="w-20 px-2 py-1 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              />
              <span className="text-xs text-slate-500">шт</span>
            </div>
            <button
              onClick={handleStockCheck}
              disabled={stockLoading}
              className="px-3 py-1.5 text-xs bg-indigo-700 text-white rounded hover:bg-indigo-600 disabled:opacity-50"
            >
              {stockLoading ? "Проверка..." : "Проверить"}
            </button>
            {stockCheck && stockCheck.shortage_count > 0 && (
              <button
                onClick={handleCreatePR}
                disabled={actionLoading}
                className="px-3 py-1.5 text-xs bg-amber-700 text-white rounded hover:bg-amber-600 disabled:opacity-50"
              >
                Создать заявку на закупку
              </button>
            )}
          </div>

          {stockCheck && (
            <div>
              <div
                className={`mb-3 px-3 py-2 rounded text-sm font-medium ${
                  stockCheck.can_produce
                    ? "bg-green-900/50 text-green-300"
                    : "bg-red-900/50 text-red-300"
                }`}
              >
                {stockCheck.can_produce
                  ? `✓ Всё в наличии — можно произвести партию ${stockCheck.batch_qty} шт`
                  : `✗ Нехватка по ${stockCheck.shortage_count} позициям для партии ${stockCheck.batch_qty} шт`}
              </div>
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-xs text-slate-500 uppercase tracking-wider border-b border-slate-700">
                    <th className="text-left py-1.5 pr-4">Позиция</th>
                    <th className="text-right py-1.5 pr-4">Требуется</th>
                    <th className="text-right py-1.5 pr-4">В наличии</th>
                    <th className="text-right py-1.5">Нехватка</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700/50">
                  {stockCheck.lines.map((line) => {
                    const hasShortage =
                      line.shortage != null && line.shortage > 0;
                    const unknown = line.available_qty == null;
                    return (
                      <tr
                        key={line.line_number}
                        className={hasShortage ? "bg-red-950/20" : ""}
                      >
                        <td className="py-2 pr-4 text-slate-300">
                          {line.description}
                        </td>
                        <td className="py-2 pr-4 text-right text-slate-200">
                          {line.required_qty} {line.unit}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {unknown ? (
                            <span className="text-slate-600 text-xs">
                              не учтено
                            </span>
                          ) : (
                            <span
                              className={
                                line.available_qty! < line.required_qty
                                  ? "text-red-400"
                                  : "text-green-400"
                              }
                            >
                              {line.available_qty} {line.unit}
                            </span>
                          )}
                        </td>
                        <td className="py-2 text-right">
                          {hasShortage ? (
                            <span className="text-red-400 font-medium">
                              −{line.shortage} {line.unit}
                            </span>
                          ) : (
                            <span className="text-slate-600">—</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {showAddLine && (
        <AddLineForm
          bomId={id}
          nextLineNumber={nextLineNumber}
          onClose={() => setShowAddLine(false)}
          onAdded={() => {
            setShowAddLine(false);
            loadBom();
          }}
        />
      )}

      {toast && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 bg-slate-700 text-white text-sm rounded-lg shadow-lg z-50">
          {toast}
        </div>
      )}
    </div>
  );
}
