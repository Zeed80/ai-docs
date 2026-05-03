"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useEffect, useState, useCallback, useRef } from "react";
import Link from "next/link";

const API = getApiBaseUrl();

// ─── Types ────────────────────────────────────────────────────────────────────

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

interface ReceiptLine {
  id: string;
  description: string;
  quantity_expected: number;
  quantity_received: number;
  unit: string;
  discrepancy_note: string | null;
  inventory_item_id: string | null;
  invoice_line_id: string | null;
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
  lines: ReceiptLine[];
  created_at: string;
  updated_at: string;
}

interface StockMovement {
  id: string;
  inventory_item_id: string;
  item_name: string | null;
  movement_type: string;
  quantity: number;
  balance_after: number;
  reference_type: string | null;
  reference_id: string | null;
  performed_by: string;
  performed_at: string;
  notes: string | null;
}

type Tab = "pending" | "inventory" | "receipts" | "movements";

// ─── Constants ────────────────────────────────────────────────────────────────

const STATUS_LABELS: Record<string, string> = {
  pending: "Ожидание",
  draft: "Черновик",
  expected: "Ожидается",
  partial: "Частично получен",
  received: "Получен",
  issued: "Выдан",
  cancelled: "Отменён",
};

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-blue-500/20 text-blue-300",
  draft: "bg-yellow-500/20 text-yellow-300",
  expected: "bg-cyan-500/20 text-cyan-300",
  partial: "bg-orange-500/20 text-orange-300",
  received: "bg-green-500/20 text-green-300",
  issued: "bg-purple-500/20 text-purple-300",
  cancelled: "bg-slate-600/40 text-slate-400",
};

const MOVEMENT_LABELS: Record<string, string> = {
  receipt: "Приход",
  issue: "Выдача",
  adjustment: "Корректировка",
};

const MOVEMENT_COLORS: Record<string, string> = {
  receipt: "text-green-400",
  issue: "text-red-400",
  adjustment: "text-yellow-400",
};

// ─── Helper ───────────────────────────────────────────────────────────────────

async function apiFetch(url: string, options?: RequestInit): Promise<Response> {
  return fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function WarehousePage() {
  const [tab, setTab] = useState<Tab>("pending");
  const [pendingCount, setPendingCount] = useState(0);

  const loadPendingCount = useCallback(() => {
    fetch(`${API}/api/warehouse/receipts?status=pending&limit=1`)
      .then((r) => r.json())
      .then((d) => setPendingCount(d.total ?? 0))
      .catch(() => {});
  }, []);

  useEffect(() => {
    loadPendingCount();
  }, [loadPendingCount]);

  const TAB_LABELS: Record<Tab, string> = {
    pending: "Ожидание",
    inventory: "Остатки",
    receipts: "Приходные ордера",
    movements: "Движения",
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Склад</h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Управление запасами, приёмка и движение товаров
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="px-6 border-b border-slate-700 flex gap-1">
        {(["pending", "inventory", "receipts", "movements"] as Tab[]).map(
          (t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`relative px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                tab === t
                  ? "border-blue-500 text-blue-400"
                  : "border-transparent text-slate-400 hover:text-slate-200"
              }`}
            >
              {TAB_LABELS[t]}
              {t === "pending" && pendingCount > 0 && (
                <span className="ml-1.5 text-[10px] bg-blue-500/30 text-blue-300 px-1.5 py-0.5 rounded-full">
                  {pendingCount}
                </span>
              )}
            </button>
          ),
        )}
      </div>

      <div className="flex-1 overflow-auto p-6">
        {tab === "pending" && <PendingTab onAccepted={loadPendingCount} />}
        {tab === "inventory" && <InventoryTab />}
        {tab === "receipts" && <ReceiptsTab />}
        {tab === "movements" && <MovementsTab />}
      </div>
    </div>
  );
}

// ─── Tab: Ожидание ─────────────────────────────────────────────────────────────

function PendingTab({ onAccepted }: { onAccepted: () => void }) {
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkLoading, setBulkLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setSelected(new Set());
    fetch(`${API}/api/warehouse/receipts?status=pending&limit=100`)
      .then((r) => r.json())
      .then((d) => {
        setReceipts(d.items ?? []);
        setTotal(d.total ?? 0);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const allSelected = receipts.length > 0 && selected.size === receipts.length;

  function toggleAll() {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(receipts.map((r) => r.id)));
    }
  }

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  async function acceptSingle(id: string) {
    setError("");
    const res = await apiFetch(`${API}/api/warehouse/receipts/${id}/confirm`, {
      method: "POST",
    });
    if (!res.ok) {
      const d = await res.json();
      setError(d.detail ?? "Ошибка подтверждения");
      return;
    }
    onAccepted();
    load();
  }

  async function cancelSingle(id: string) {
    setError("");
    const res = await apiFetch(`${API}/api/warehouse/receipts/${id}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const d = await res.json();
      setError(d.detail ?? "Ошибка отмены");
      return;
    }
    onAccepted();
    load();
  }

  async function bulkAccept() {
    if (selected.size === 0) return;
    setBulkLoading(true);
    setError("");
    try {
      const res = await apiFetch(`${API}/api/warehouse/receipts/bulk-confirm`, {
        method: "POST",
        body: JSON.stringify({ receipt_ids: Array.from(selected) }),
      });
      const d = await res.json();
      if (d.failed && d.failed.length > 0) {
        setError(
          `Ошибки: ${d.failed.map((f: { id: string; error: string }) => f.error).join("; ")}`,
        );
      }
      onAccepted();
      load();
    } catch {
      setError("Ошибка сети");
    } finally {
      setBulkLoading(false);
    }
  }

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4">
        <span className="text-xs text-slate-500">
          {total} ордеров в очереди
        </span>
        {selected.size > 0 && (
          <>
            <span className="text-xs text-blue-400">
              Выбрано: {selected.size}
            </span>
            <button
              onClick={bulkAccept}
              disabled={bulkLoading}
              className="px-3 py-1.5 text-xs bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white rounded transition-colors"
            >
              {bulkLoading
                ? "Принимаю..."
                : `Принять выбранные (${selected.size})`}
            </button>
          </>
        )}
        {error && <span className="text-xs text-red-400 ml-2">{error}</span>}
      </div>

      {loading ? (
        <p className="text-slate-400 text-sm">Загрузка...</p>
      ) : receipts.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p className="text-4xl mb-3">✅</p>
          <p className="text-sm">Нет ожидающих ордеров</p>
          <p className="text-xs mt-2 text-slate-600">
            Ордера появятся автоматически после одобрения счёта
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {/* Select all header */}
          <div className="flex items-center gap-3 pb-2 border-b border-slate-700">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={toggleAll}
              className="w-3.5 h-3.5 accent-blue-500"
            />
            <span className="text-xs text-slate-500">
              Выбрать все ({receipts.length})
            </span>
          </div>

          {receipts.map((receipt) => (
            <PendingReceiptCard
              key={receipt.id}
              receipt={receipt}
              selected={selected.has(receipt.id)}
              onToggle={() => toggle(receipt.id)}
              onAccept={() => acceptSingle(receipt.id)}
              onCancel={() => cancelSingle(receipt.id)}
              onLineUpdated={load}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function PendingReceiptCard({
  receipt,
  selected,
  onToggle,
  onAccept,
  onCancel,
  onLineUpdated,
}: {
  receipt: Receipt;
  selected: boolean;
  onToggle: () => void;
  onAccept: () => void;
  onCancel: () => void;
  onLineUpdated: () => void;
}) {
  const [accepting, setAccepting] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  async function handleAccept() {
    setAccepting(true);
    await onAccept();
    setAccepting(false);
  }

  async function handleCancel() {
    setCancelling(true);
    await onCancel();
    setCancelling(false);
  }

  return (
    <div
      className={`border rounded-lg transition-colors ${
        selected
          ? "border-blue-500/50 bg-blue-500/5"
          : "border-slate-700 bg-slate-800/50"
      }`}
    >
      {/* Card header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-slate-700/60">
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggle}
          className="w-3.5 h-3.5 accent-blue-500 flex-shrink-0"
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-mono font-medium text-slate-200">
              {receipt.receipt_number ?? receipt.id.slice(0, 8)}
            </span>
            <span className="text-[10px] px-2 py-0.5 rounded-full bg-blue-500/20 text-blue-300">
              Ожидание
            </span>
            <span className="text-xs text-slate-500">
              {new Date(receipt.created_at).toLocaleDateString("ru-RU")}
            </span>
          </div>
          <div className="flex gap-4 mt-0.5 text-[11px] text-slate-500">
            <span>{receipt.lines.length} позиций</span>
            {receipt.invoice_id && (
              <Link
                href={`/invoices`}
                className="text-blue-400 hover:text-blue-300"
              >
                По счёту →
              </Link>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            onClick={handleCancel}
            disabled={cancelling || accepting}
            className="px-3 py-1 text-xs border border-slate-600 text-slate-400 hover:text-red-400 hover:border-red-600 rounded transition-colors disabled:opacity-50"
          >
            {cancelling ? "..." : "Отклонить"}
          </button>
          <button
            onClick={handleAccept}
            disabled={accepting || cancelling}
            className="px-3 py-1 text-xs bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white rounded transition-colors"
          >
            {accepting ? "Принимаю..." : "Принять"}
          </button>
          <Link
            href={`/warehouse/receipts/${receipt.id}`}
            className="text-xs text-blue-400 hover:text-blue-300 px-2"
          >
            Детали →
          </Link>
        </div>
      </div>

      {/* Lines table (inline editing) */}
      {receipt.lines.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="text-[10px] text-slate-600 border-b border-slate-700/60">
                <th className="px-4 py-1.5 text-left font-medium">
                  Наименование
                </th>
                <th className="px-3 py-1.5 text-right font-medium">Ожид.</th>
                <th className="px-3 py-1.5 text-right font-medium">Факт</th>
                <th className="px-3 py-1.5 text-left font-medium">Ед.</th>
                <th className="px-4 py-1.5 text-left font-medium">
                  Примечание
                </th>
              </tr>
            </thead>
            <tbody>
              {receipt.lines.map((line) => (
                <PendingLineRow
                  key={line.id}
                  line={line}
                  receiptId={receipt.id}
                  onSaved={onLineUpdated}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PendingLineRow({
  line,
  receiptId,
  onSaved,
}: {
  line: ReceiptLine;
  receiptId: string;
  onSaved: () => void;
}) {
  const [qty, setQty] = useState(String(line.quantity_received));
  const [note, setNote] = useState(line.discrepancy_note ?? "");
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const qtyNum = parseFloat(qty) || 0;
  const hasDiscrepancy = qtyNum !== line.quantity_expected;

  async function save() {
    setSaving(true);
    try {
      await apiFetch(
        `${API}/api/warehouse/receipts/${receiptId}/lines/${line.id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            quantity_received: qtyNum,
            discrepancy_note: note || null,
          }),
        },
      );
      setDirty(false);
      onSaved();
    } finally {
      setSaving(false);
    }
  }

  function onQtyChange(v: string) {
    setQty(v);
    setDirty(true);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      save();
    }, 800);
  }

  return (
    <tr
      className={`border-b border-slate-700/30 last:border-0 hover:bg-slate-800/40 ${
        hasDiscrepancy && qtyNum > 0 ? "bg-orange-950/10" : ""
      }`}
    >
      <td className="px-4 py-1.5 text-slate-300">{line.description}</td>
      <td className="px-3 py-1.5 text-right text-slate-500 font-mono">
        {line.quantity_expected}
      </td>
      <td className="px-3 py-1.5 text-right">
        <input
          type="number"
          min="0"
          step="any"
          value={qty}
          onChange={(e) => onQtyChange(e.target.value)}
          className={`w-20 px-1.5 py-0.5 text-xs text-right font-mono bg-slate-700 border rounded focus:outline-none focus:border-blue-500 ${
            hasDiscrepancy && qtyNum > 0
              ? "border-orange-600 text-orange-300"
              : "border-slate-600 text-slate-200"
          }`}
        />
      </td>
      <td className="px-3 py-1.5 text-slate-500">{line.unit}</td>
      <td className="px-4 py-1.5">
        <div className="flex items-center gap-1">
          <input
            value={note}
            onChange={(e) => {
              setNote(e.target.value);
              setDirty(true);
            }}
            onBlur={() => dirty && save()}
            placeholder={hasDiscrepancy ? "Причина..." : ""}
            className="flex-1 min-w-0 px-1.5 py-0.5 text-xs bg-slate-700 border border-slate-600 rounded text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500"
          />
          {saving && (
            <span className="text-[9px] text-slate-500 flex-shrink-0">⏳</span>
          )}
        </div>
      </td>
    </tr>
  );
}

// ─── Tab: Остатки ─────────────────────────────────────────────────────────────

function InventoryTab() {
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [lowStockOnly, setLowStockOnly] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [editItem, setEditItem] = useState<InventoryItem | null>(null);
  const [issueItem, setIssueItem] = useState<InventoryItem | null>(null);
  const [adjustItem, setAdjustItem] = useState<InventoryItem | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<InventoryItem | null>(
    null,
  );
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(() => {
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
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [search, lowStockOnly]);

  useEffect(() => {
    load();
  }, [load]);

  const lowStockCount = items.filter((i) => i.is_low_stock).length;

  async function deleteItem(item: InventoryItem) {
    setDeleting(true);
    setError("");
    try {
      const res = await apiFetch(`${API}/api/warehouse/inventory/${item.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка удаления");
        return;
      }
      setDeleteConfirm(null);
      load();
    } catch {
      setError("Ошибка сети");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4 flex-wrap">
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
              <th className="pb-2 pr-4 font-medium">Место</th>
              <th className="pb-2 font-medium text-right">Действия</th>
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
                <td className="py-2 pr-4 text-slate-400 text-xs">
                  {item.location ?? "—"}
                </td>
                <td className="py-2 text-right">
                  <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button
                      onClick={() => setEditItem(item)}
                      title="Изменить"
                      className="px-2 py-1 text-[10px] text-slate-400 hover:text-blue-400 hover:bg-blue-500/10 rounded transition-colors"
                    >
                      Изменить
                    </button>
                    <button
                      onClick={() => setIssueItem(item)}
                      title="Выдать"
                      className="px-2 py-1 text-[10px] text-slate-400 hover:text-orange-400 hover:bg-orange-500/10 rounded transition-colors"
                    >
                      Выдать
                    </button>
                    <button
                      onClick={() => setAdjustItem(item)}
                      title="Корректировка"
                      className="px-2 py-1 text-[10px] text-slate-400 hover:text-yellow-400 hover:bg-yellow-500/10 rounded transition-colors"
                    >
                      Корр.
                    </button>
                    <button
                      onClick={() => setDeleteConfirm(item)}
                      title="Удалить"
                      className="px-2 py-1 text-[10px] text-slate-400 hover:text-red-400 hover:bg-red-500/10 rounded transition-colors"
                    >
                      ✕
                    </button>
                  </div>
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
      {editItem && (
        <EditItemModal
          item={editItem}
          onClose={() => {
            setEditItem(null);
            load();
          }}
        />
      )}
      {issueItem && (
        <IssueModal
          item={issueItem}
          onClose={() => {
            setIssueItem(null);
            load();
          }}
        />
      )}
      {adjustItem && (
        <AdjustModal
          item={adjustItem}
          onClose={() => {
            setAdjustItem(null);
            load();
          }}
        />
      )}

      {/* Delete confirm dialog */}
      {deleteConfirm && (
        <div
          className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
          onClick={() => setDeleteConfirm(null)}
        >
          <div
            className="bg-slate-800 border border-slate-600 rounded-lg p-6 w-full max-w-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold text-slate-100 mb-2">
              Удалить позицию?
            </h2>
            <p className="text-sm text-slate-400 mb-1">
              <span className="font-medium text-slate-200">
                {deleteConfirm.name}
              </span>
            </p>
            {deleteConfirm.current_qty !== 0 && (
              <p className="text-xs text-orange-400 mb-3">
                Внимание: текущий остаток{" "}
                <span className="font-mono font-semibold">
                  {deleteConfirm.current_qty} {deleteConfirm.unit}
                </span>{" "}
                будет удалён вместе с позицией.
              </p>
            )}
            {error && <p className="text-xs text-red-400 mb-3">{error}</p>}
            <div className="flex justify-end gap-2">
              <button
                onClick={() => {
                  setDeleteConfirm(null);
                  setError("");
                }}
                className="px-4 py-1.5 text-sm text-slate-400 hover:text-slate-200"
              >
                Отмена
              </button>
              <button
                onClick={() => deleteItem(deleteConfirm)}
                disabled={deleting}
                className="px-4 py-1.5 text-sm bg-red-700 hover:bg-red-600 disabled:opacity-50 text-white rounded transition-colors"
              >
                {deleting ? "Удаляю..." : "Удалить"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Tab: Приходные ордера ────────────────────────────────────────────────────

function ReceiptsTab() {
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("received");

  const load = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams({ limit: "100" });
    if (statusFilter) {
      params.set("status", statusFilter);
    } else {
      params.set("exclude_status", "pending");
    }
    fetch(`${API}/api/warehouse/receipts?${params}`)
      .then((r) => r.json())
      .then((d) => {
        setReceipts(d.items ?? []);
        setTotal(d.total ?? 0);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [statusFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-3 py-1.5 text-sm bg-slate-800 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
        >
          <option value="">Все (кроме очереди)</option>
          <option value="draft">Черновик</option>
          <option value="expected">Ожидается</option>
          <option value="partial">Частично получен</option>
          <option value="received">Получен</option>
          <option value="issued">Выдан</option>
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
          <p className="text-xs mt-2">
            Создайте ордер из карточки счёта или смотрите вкладку «Ожидание»
          </p>
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

// ─── Tab: Движения ────────────────────────────────────────────────────────────

function MovementsTab() {
  const [movements, setMovements] = useState<StockMovement[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [typeFilter, setTypeFilter] = useState<string>("");

  const load = useCallback(() => {
    setLoading(true);
    const params = new URLSearchParams({ limit: "200" });
    if (typeFilter) params.set("movement_type", typeFilter);
    fetch(`${API}/api/warehouse/movements?${params}`)
      .then((r) => r.json())
      .then((d) => {
        setMovements(d.items ?? []);
        setTotal(d.total ?? 0);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [typeFilter]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      {/* Toolbar */}
      <div className="flex items-center gap-3 mb-4">
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="px-3 py-1.5 text-sm bg-slate-800 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
        >
          <option value="">Все типы</option>
          <option value="receipt">Приход</option>
          <option value="issue">Выдача</option>
          <option value="adjustment">Корректировка</option>
        </select>
        <span className="ml-auto text-xs text-slate-500">{total} движений</span>
      </div>

      {loading ? (
        <p className="text-slate-400 text-sm">Загрузка...</p>
      ) : movements.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p className="text-4xl mb-3">📈</p>
          <p>Движений нет</p>
        </div>
      ) : (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-left text-[11px] text-slate-500 border-b border-slate-700">
              <th className="pb-2 pr-4 font-medium">Дата</th>
              <th className="pb-2 pr-4 font-medium">Позиция</th>
              <th className="pb-2 pr-4 font-medium">Тип</th>
              <th className="pb-2 pr-4 font-medium text-right">Кол-во</th>
              <th className="pb-2 pr-4 font-medium text-right">Остаток</th>
              <th className="pb-2 pr-4 font-medium">Исполнитель</th>
              <th className="pb-2 font-medium">Примечание</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {movements.map((m) => (
              <tr
                key={m.id}
                className="hover:bg-slate-800/40 transition-colors"
              >
                <td className="py-2 pr-4 text-slate-400 text-xs whitespace-nowrap">
                  {new Date(m.performed_at).toLocaleString("ru-RU", {
                    day: "2-digit",
                    month: "2-digit",
                    year: "2-digit",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </td>
                <td className="py-2 pr-4 text-slate-200 max-w-xs truncate">
                  {m.item_name ?? m.inventory_item_id.slice(0, 8)}
                </td>
                <td className="py-2 pr-4">
                  <span
                    className={`text-xs font-medium ${MOVEMENT_COLORS[m.movement_type] ?? "text-slate-400"}`}
                  >
                    {MOVEMENT_LABELS[m.movement_type] ?? m.movement_type}
                  </span>
                </td>
                <td
                  className={`py-2 pr-4 text-right font-mono text-sm font-semibold ${
                    m.quantity > 0 ? "text-green-400" : "text-red-400"
                  }`}
                >
                  {m.quantity > 0 ? "+" : ""}
                  {m.quantity.toLocaleString("ru-RU")}
                </td>
                <td className="py-2 pr-4 text-right font-mono text-xs text-slate-300">
                  {m.balance_after.toLocaleString("ru-RU")}
                </td>
                <td className="py-2 pr-4 text-slate-400 text-xs">
                  {m.performed_by}
                </td>
                <td className="py-2 text-slate-500 text-xs max-w-xs truncate">
                  {m.notes ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ─── Modals ───────────────────────────────────────────────────────────────────

function ModalWrapper({
  onClose,
  children,
}: {
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <div
        className="bg-slate-800 border border-slate-600 rounded-lg p-6 w-full max-w-md shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

function CreateItemModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [unit, setUnit] = useState("шт");
  const [sku, setSku] = useState("");
  const [location, setLocation] = useState("");
  const [minQty, setMinQty] = useState("");
  const [currentQty, setCurrentQty] = useState("0");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError("");
    try {
      const res = await apiFetch(`${API}/api/warehouse/inventory`, {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          unit: unit.trim() || "шт",
          sku: sku.trim() || null,
          location: location.trim() || null,
          min_qty: minQty ? parseFloat(minQty) : null,
          current_qty: parseFloat(currentQty) || 0,
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
    <ModalWrapper onClose={onClose}>
      <form onSubmit={submit}>
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
              autoFocus
              className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-slate-400 mb-1">
                Единица
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
            <div className="w-28">
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
            <div className="w-28">
              <label className="block text-xs text-slate-400 mb-1">
                Нач. остаток
              </label>
              <input
                value={currentQty}
                onChange={(e) => setCurrentQty(e.target.value)}
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
    </ModalWrapper>
  );
}

function EditItemModal({
  item,
  onClose,
}: {
  item: InventoryItem;
  onClose: () => void;
}) {
  const [name, setName] = useState(item.name);
  const [unit, setUnit] = useState(item.unit);
  const [sku, setSku] = useState(item.sku ?? "");
  const [location, setLocation] = useState(item.location ?? "");
  const [minQty, setMinQty] = useState(
    item.min_qty != null ? String(item.min_qty) : "",
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError("");
    try {
      const res = await apiFetch(`${API}/api/warehouse/inventory/${item.id}`, {
        method: "PATCH",
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
    <ModalWrapper onClose={onClose}>
      <form onSubmit={submit}>
        <h2 className="text-base font-semibold text-slate-100 mb-1">
          Изменить позицию
        </h2>
        <p className="text-xs text-slate-500 mb-4">
          Текущий остаток: {item.current_qty} {item.unit}
        </p>
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
              autoFocus
              className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-slate-400 mb-1">
                Единица
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
            <div className="w-32">
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
            {saving ? "Сохранение..." : "Сохранить"}
          </button>
        </div>
      </form>
    </ModalWrapper>
  );
}

function IssueModal({
  item,
  onClose,
}: {
  item: InventoryItem;
  onClose: () => void;
}) {
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("");
  const [performedBy, setPerformedBy] = useState("user");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const qtyNum = parseFloat(qty);
    if (!qtyNum || qtyNum <= 0) {
      setError("Укажите положительное количество");
      return;
    }
    if (!reason.trim()) {
      setError("Укажите причину выдачи");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const res = await apiFetch(
        `${API}/api/warehouse/inventory/${item.id}/issue`,
        {
          method: "POST",
          body: JSON.stringify({
            quantity: qtyNum,
            reason: reason.trim(),
            performed_by: performedBy.trim() || "user",
          }),
        },
      );
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
    <ModalWrapper onClose={onClose}>
      <form onSubmit={submit}>
        <h2 className="text-base font-semibold text-slate-100 mb-1">
          Выдача со склада
        </h2>
        <p className="text-xs text-slate-500 mb-4">
          {item.name} — остаток: {item.current_qty} {item.unit}
        </p>
        {error && <p className="text-red-400 text-xs mb-3">{error}</p>}
        <div className="space-y-3">
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-slate-400 mb-1">
                Количество *
              </label>
              <input
                value={qty}
                onChange={(e) => setQty(e.target.value)}
                type="number"
                min="0.001"
                step="any"
                max={item.current_qty}
                required
                autoFocus
                placeholder={`макс. ${item.current_qty}`}
                className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
              />
            </div>
            <div className="w-16 flex items-end pb-1.5">
              <span className="text-sm text-slate-500">{item.unit}</span>
            </div>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Причина выдачи *
            </label>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Для производства / цех №2 / ..."
              required
              className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">Выдал</label>
            <input
              value={performedBy}
              onChange={(e) => setPerformedBy(e.target.value)}
              className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
            />
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
            disabled={saving}
            className="px-4 py-1.5 text-sm bg-orange-700 hover:bg-orange-600 disabled:opacity-50 text-white rounded transition-colors"
          >
            {saving ? "Выдаю..." : "Выдать"}
          </button>
        </div>
      </form>
    </ModalWrapper>
  );
}

function AdjustModal({
  item,
  onClose,
}: {
  item: InventoryItem;
  onClose: () => void;
}) {
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("");
  const [performedBy, setPerformedBy] = useState("user");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const qtyNum = parseFloat(qty) || 0;
  const newBalance = item.current_qty + qtyNum;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (qtyNum === 0) {
      setError("Укажите ненулевое количество (+ или -)");
      return;
    }
    if (!reason.trim()) {
      setError("Укажите причину корректировки");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const res = await apiFetch(
        `${API}/api/warehouse/inventory/${item.id}/adjust`,
        {
          method: "POST",
          body: JSON.stringify({
            quantity: qtyNum,
            reason: reason.trim(),
            performed_by: performedBy.trim() || "user",
          }),
        },
      );
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
    <ModalWrapper onClose={onClose}>
      <form onSubmit={submit}>
        <h2 className="text-base font-semibold text-slate-100 mb-1">
          Корректировка остатка
        </h2>
        <p className="text-xs text-slate-500 mb-4">
          {item.name} — текущий остаток: {item.current_qty} {item.unit}
        </p>
        {error && <p className="text-red-400 text-xs mb-3">{error}</p>}
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Изменение (+ или −) *
            </label>
            <div className="flex gap-2 items-center">
              <input
                value={qty}
                onChange={(e) => setQty(e.target.value)}
                type="number"
                step="any"
                required
                autoFocus
                placeholder="+10 или -5"
                className="flex-1 px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
              />
              <span className="text-sm text-slate-500">{item.unit}</span>
            </div>
            {qty && (
              <p
                className={`text-xs mt-1 ${newBalance < 0 ? "text-red-400" : "text-slate-500"}`}
              >
                Новый остаток:{" "}
                <span
                  className={`font-mono font-semibold ${newBalance < 0 ? "text-red-400" : "text-slate-200"}`}
                >
                  {newBalance.toLocaleString("ru-RU")}
                </span>{" "}
                {item.unit}
              </p>
            )}
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Причина *
            </label>
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Инвентаризация / брак / пересчёт..."
              required
              className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Кто корректирует
            </label>
            <input
              value={performedBy}
              onChange={(e) => setPerformedBy(e.target.value)}
              className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
            />
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
            disabled={saving || qtyNum === 0 || newBalance < 0}
            className="px-4 py-1.5 text-sm bg-yellow-700 hover:bg-yellow-600 disabled:opacity-50 text-white rounded transition-colors"
          >
            {saving ? "Применяю..." : "Применить"}
          </button>
        </div>
      </form>
    </ModalWrapper>
  );
}
