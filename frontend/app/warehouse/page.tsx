"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface InventoryItem {
  id: string;
  sku: string | null;
  name: string;
  unit: string;
  current_qty: number;
  min_qty: number | null;
  location: string | null;
  is_low_stock: boolean;
}

interface Receipt {
  id: string;
  receipt_number: string | null;
  status: string;
  received_at: string;
  received_by: string | null;
  notes: string | null;
  invoice_id: string | null;
  supplier_id: string | null;
  lines: { id: string }[];
}

type Tab = "inventory" | "receipts";

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  confirmed: "Подтверждён",
  cancelled: "Отменён",
};

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-yellow-500/20 text-yellow-300",
  confirmed: "bg-green-500/20 text-green-300",
  cancelled: "bg-slate-600/40 text-slate-400",
};

export default function WarehousePage() {
  const [tab, setTab] = useState<Tab>("inventory");

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Склад</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Остатки и приходные ордера
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="px-6 border-b border-slate-700 flex gap-1">
        {(["inventory", "receipts"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
              tab === t
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            {t === "inventory" ? "Остатки" : "Приходные ордера"}
          </button>
        ))}
      </div>

      <div className="flex-1 overflow-auto p-6">
        {tab === "inventory" ? <InventoryTab /> : <ReceiptsTab />}
      </div>
    </div>
  );
}

function InventoryTab() {
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [lowStockOnly, setLowStockOnly] = useState(false);
  const [showCreate, setShowCreate] = useState(false);

  function load() {
    setLoading(true);
    const params = new URLSearchParams({ limit: "100" });
    if (search) params.set("search", search);
    if (lowStockOnly) params.set("low_stock", "true");
    fetch(`${API}/api/warehouse/inventory?${params}`)
      .then((r) => r.json())
      .then((d) => {
        setItems(d.items ?? []);
        setTotal(d.total ?? 0);
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, [search, lowStockOnly]);

  const lowStockCount = items.filter((i) => i.is_low_stock).length;

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Поиск по наименованию..."
          className="flex-1 max-w-xs px-3 py-1.5 text-sm bg-slate-800 border border-slate-600 rounded text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500"
        />
        <label className="flex items-center gap-2 text-sm text-slate-300 cursor-pointer">
          <input
            type="checkbox"
            checked={lowStockOnly}
            onChange={(e) => setLowStockOnly(e.target.checked)}
            className="w-3.5 h-3.5 accent-orange-500"
          />
          Только дефицит
          {lowStockCount > 0 && (
            <span className="text-[10px] bg-orange-500/20 text-orange-300 px-1.5 py-0.5 rounded-full">
              {lowStockCount}
            </span>
          )}
        </label>
        <span className="ml-auto text-xs text-slate-500">{total} позиций</span>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 text-xs bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors"
        >
          + Добавить позицию
        </button>
      </div>

      {loading ? (
        <p className="text-slate-400 text-sm">Загрузка...</p>
      ) : items.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p className="text-4xl mb-3">📦</p>
          <p>Позиции не найдены</p>
        </div>
      ) : (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-left text-[11px] text-slate-500 border-b border-slate-700">
              <th className="pb-2 pr-4 font-medium">Наименование</th>
              <th className="pb-2 pr-4 font-medium">Артикул</th>
              <th className="pb-2 pr-4 font-medium text-right">Остаток</th>
              <th className="pb-2 pr-4 font-medium text-right">Мин.</th>
              <th className="pb-2 font-medium">Место хранения</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {items.map((item) => (
              <tr
                key={item.id}
                className="hover:bg-slate-800/40 transition-colors group"
              >
                <td className="py-2 pr-4">
                  <span className="text-slate-200 font-medium">
                    {item.name}
                  </span>
                  {item.is_low_stock && (
                    <span className="ml-2 text-[9px] bg-orange-500/20 text-orange-300 px-1.5 py-0.5 rounded-full">
                      дефицит
                    </span>
                  )}
                </td>
                <td className="py-2 pr-4 text-slate-500 font-mono text-xs">
                  {item.sku ?? "—"}
                </td>
                <td
                  className={`py-2 pr-4 text-right font-mono font-semibold ${
                    item.is_low_stock ? "text-orange-400" : "text-slate-200"
                  }`}
                >
                  {item.current_qty.toLocaleString("ru-RU")}
                  <span className="text-slate-500 text-xs ml-1">
                    {item.unit}
                  </span>
                </td>
                <td className="py-2 pr-4 text-right text-slate-500 font-mono text-xs">
                  {item.min_qty != null ? (
                    <>
                      {item.min_qty.toLocaleString("ru-RU")}{" "}
                      <span>{item.unit}</span>
                    </>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="py-2 text-slate-400 text-xs">
                  {item.location ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {showCreate && (
        <CreateItemModal
          onClose={() => {
            setShowCreate(false);
            load();
          }}
        />
      )}
    </div>
  );
}

function ReceiptsTab() {
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");

  function load() {
    setLoading(true);
    const params = new URLSearchParams({ limit: "100" });
    if (statusFilter) params.set("status", statusFilter);
    fetch(`${API}/api/warehouse/receipts?${params}`)
      .then((r) => r.json())
      .then((d) => {
        setReceipts(d.items ?? []);
        setTotal(d.total ?? 0);
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, [statusFilter]);

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-3 py-1.5 text-sm bg-slate-800 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
        >
          <option value="">Все статусы</option>
          <option value="draft">Черновик</option>
          <option value="confirmed">Подтверждён</option>
          <option value="cancelled">Отменён</option>
        </select>
        <span className="ml-auto text-xs text-slate-500">{total} ордеров</span>
      </div>

      {loading ? (
        <p className="text-slate-400 text-sm">Загрузка...</p>
      ) : receipts.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p className="text-4xl mb-3">📋</p>
          <p>Приходных ордеров нет</p>
          <p className="text-xs mt-2">Создайте ордер из карточки счёта</p>
        </div>
      ) : (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-left text-[11px] text-slate-500 border-b border-slate-700">
              <th className="pb-2 pr-4 font-medium">Номер</th>
              <th className="pb-2 pr-4 font-medium">Статус</th>
              <th className="pb-2 pr-4 font-medium">Дата</th>
              <th className="pb-2 pr-4 font-medium">Принял</th>
              <th className="pb-2 pr-4 font-medium text-right">Строк</th>
              <th className="pb-2 font-medium"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {receipts.map((r) => (
              <tr
                key={r.id}
                className="hover:bg-slate-800/40 transition-colors"
              >
                <td className="py-2 pr-4">
                  <span className="text-slate-200 font-mono">
                    {r.receipt_number ?? r.id.slice(0, 8)}
                  </span>
                </td>
                <td className="py-2 pr-4">
                  <span
                    className={`text-[10px] px-2 py-0.5 rounded-full ${STATUS_COLORS[r.status] ?? "bg-slate-700 text-slate-300"}`}
                  >
                    {STATUS_LABELS[r.status] ?? r.status}
                  </span>
                </td>
                <td className="py-2 pr-4 text-slate-400 text-xs">
                  {new Date(r.received_at).toLocaleDateString("ru-RU")}
                </td>
                <td className="py-2 pr-4 text-slate-400 text-xs">
                  {r.received_by ?? "—"}
                </td>
                <td className="py-2 pr-4 text-right text-slate-300">
                  {r.lines.length}
                </td>
                <td className="py-2">
                  <Link
                    href={`/warehouse/receipts/${r.id}`}
                    className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
                  >
                    Открыть →
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function CreateItemModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [unit, setUnit] = useState("шт");
  const [sku, setSku] = useState("");
  const [location, setLocation] = useState("");
  const [minQty, setMinQty] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError("");
    try {
      const res = await fetch(`${API}/api/warehouse/inventory`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          unit: unit.trim() || "шт",
          sku: sku.trim() || null,
          location: location.trim() || null,
          min_qty: minQty ? parseFloat(minQty) : null,
        }),
      });
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка");
        return;
      }
      onClose();
    } catch {
      setError("Ошибка сети");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="bg-slate-800 border border-slate-600 rounded-lg p-6 w-full max-w-md"
      >
        <h2 className="text-base font-semibold text-slate-100 mb-4">
          Новая позиция склада
        </h2>
        {error && <p className="text-red-400 text-xs mb-3">{error}</p>}
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Наименование *
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-slate-400 mb-1">
                Единица измерения
              </label>
              <input
                value={unit}
                onChange={(e) => setUnit(e.target.value)}
                className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
              />
            </div>
            <div className="flex-1">
              <label className="block text-xs text-slate-400 mb-1">
                Артикул
              </label>
              <input
                value={sku}
                onChange={(e) => setSku(e.target.value)}
                className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-slate-400 mb-1">
                Место хранения
              </label>
              <input
                value={location}
                onChange={(e) => setLocation(e.target.value)}
                placeholder="Склад А / Стеллаж 3"
                className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
              />
            </div>
            <div className="flex-1">
              <label className="block text-xs text-slate-400 mb-1">
                Мин. остаток
              </label>
              <input
                value={minQty}
                onChange={(e) => setMinQty(e.target.value)}
                type="number"
                min="0"
                step="any"
                className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-1.5 text-sm text-slate-400 hover:text-slate-200 transition-colors"
          >
            Отмена
          </button>
          <button
            type="submit"
            disabled={saving || !name.trim()}
            className="px-4 py-1.5 text-sm bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded transition-colors"
          >
            {saving ? "Сохранение..." : "Создать"}
          </button>
        </div>
      </form>
    </div>
  );
}
