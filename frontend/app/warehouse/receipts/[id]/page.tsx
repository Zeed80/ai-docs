"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";

const API = getApiBaseUrl();

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

const STATUS_LABELS: Record<string, string> = {
  pending: "Ожидание",
  draft: "Черновик",
  expected: "Ожидается",
  partial: "Частично получен",
  received: "Получен",
  confirmed: "Подтверждён",
  issued: "Выдан",
  cancelled: "Отменён",
};

const STATUS_COLORS: Record<string, string> = {
  pending: "bg-blue-500/20 text-blue-300",
  draft: "bg-yellow-500/20 text-yellow-300",
  expected: "bg-cyan-500/20 text-cyan-300",
  partial: "bg-orange-500/20 text-orange-300",
  received: "bg-green-500/20 text-green-300",
  confirmed: "bg-green-500/20 text-green-300",
  issued: "bg-purple-500/20 text-purple-300",
  cancelled: "bg-slate-600/40 text-slate-400",
};

export default function ReceiptDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [loading, setLoading] = useState(true);
  const [confirming, setConfirming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [confirmDialog, setConfirmDialog] = useState(false);
  const [error, setError] = useState("");

  function load() {
    setLoading(true);
    fetch(`${API}/api/warehouse/receipts/${id}`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        if (data && data.id) setReceipt(data);
        else setReceipt(null);
      })
      .catch(() => setReceipt(null))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, [id]);

  async function updateLine(lineId: string, qty: number, note: string | null) {
    await fetch(`${API}/api/warehouse/receipts/${id}/lines/${lineId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        quantity_received: qty,
        discrepancy_note: note || null,
      }),
    });
    load();
  }

  async function confirmReceipt() {
    setConfirming(true);
    setError("");
    try {
      const res = await fetch(`${API}/api/warehouse/receipts/${id}/confirm`, {
        method: "POST",
      });
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка подтверждения");
        return;
      }
      setConfirmDialog(false);
      load();
    } catch {
      setError("Ошибка сети");
    } finally {
      setConfirming(false);
    }
  }

  async function cancelReceipt() {
    setCancelling(true);
    try {
      await fetch(`${API}/api/warehouse/receipts/${id}`, { method: "DELETE" });
      router.push("/warehouse");
    } finally {
      setCancelling(false);
    }
  }

  if (loading) {
    return <div className="p-6 text-slate-400 text-sm">Загрузка...</div>;
  }

  if (!receipt) {
    return (
      <div className="p-6 text-slate-400 text-sm">
        Ордер не найден.{" "}
        <Link href="/warehouse" className="text-blue-400 hover:underline">
          ← На склад
        </Link>
      </div>
    );
  }

  const isEditable = ["pending", "draft", "expected", "partial"].includes(
    receipt.status,
  );
  const isFinal = ["received", "confirmed", "issued", "cancelled"].includes(
    receipt.status,
  );
  const lines = receipt.lines ?? [];
  const totalExpected = lines.reduce((s, l) => s + l.quantity_expected, 0);
  const totalReceived = lines.reduce((s, l) => s + l.quantity_received, 0);
  const hasDiscrepancy = lines.some(
    (l) => l.quantity_received !== l.quantity_expected,
  );

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-6 py-4 border-b border-slate-700 flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Link
              href="/warehouse"
              className="text-slate-500 hover:text-slate-300 text-xs"
            >
              ← Склад
            </Link>
          </div>
          <div className="flex items-center gap-3">
            <h1 className="text-lg font-semibold text-slate-100">
              {receipt.receipt_number ?? `Ордер ${receipt.id.slice(0, 8)}`}
            </h1>
            <span
              className={`text-[10px] px-2 py-0.5 rounded-full ${STATUS_COLORS[receipt.status] ?? "bg-slate-700 text-slate-300"}`}
            >
              {STATUS_LABELS[receipt.status] ?? receipt.status}
            </span>
          </div>
          <div className="flex gap-4 mt-1 text-xs text-slate-500">
            <span>
              Дата: {new Date(receipt.received_at).toLocaleDateString("ru-RU")}
            </span>
            {receipt.received_by && <span>Принял: {receipt.received_by}</span>}
            {receipt.invoice_id && (
              <Link
                href={`/invoices`}
                className="text-blue-400 hover:text-blue-300"
              >
                По счёту
              </Link>
            )}
          </div>
        </div>

        {isEditable && (
          <div className="flex gap-2">
            <button
              onClick={cancelReceipt}
              disabled={cancelling}
              className="px-3 py-1.5 text-xs border border-slate-600 text-slate-400 hover:text-slate-200 hover:border-slate-500 rounded transition-colors disabled:opacity-50"
            >
              Отменить
            </button>
            <button
              onClick={() => setConfirmDialog(true)}
              className="px-4 py-1.5 text-xs bg-green-600 hover:bg-green-500 text-white rounded transition-colors"
            >
              Подтвердить приход
            </button>
          </div>
        )}
        {isFinal && (
          <span className="text-xs px-3 py-1.5 border border-slate-700 text-slate-500 rounded">
            {STATUS_LABELS[receipt.status] ?? receipt.status}
          </span>
        )}
      </div>

      {/* Summary */}
      <div className="px-6 py-3 border-b border-slate-700 flex gap-6 text-sm">
        <div>
          <span className="text-slate-500 text-xs">Ожидалось</span>
          <p className="text-slate-200 font-mono font-semibold">
            {totalExpected.toLocaleString("ru-RU")}
          </p>
        </div>
        <div>
          <span className="text-slate-500 text-xs">Получено</span>
          <p
            className={`font-mono font-semibold ${
              hasDiscrepancy ? "text-orange-400" : "text-green-400"
            }`}
          >
            {totalReceived.toLocaleString("ru-RU")}
          </p>
        </div>
        <div>
          <span className="text-slate-500 text-xs">Строк</span>
          <p className="text-slate-200 font-mono font-semibold">
            {lines.length}
          </p>
        </div>
        {hasDiscrepancy && (
          <div className="ml-auto flex items-center">
            <span className="text-xs text-orange-400 bg-orange-500/10 px-2 py-1 rounded">
              Есть расхождения — заполните примечания
            </span>
          </div>
        )}
      </div>

      {/* Lines */}
      <div className="flex-1 overflow-auto">
        {lines.length === 0 ? (
          <div className="text-center py-16 text-slate-500 text-sm">
            Строки не найдены
          </div>
        ) : (
          <table className="w-full text-sm border-collapse">
            <thead className="sticky top-0 bg-slate-900">
              <tr className="text-left text-[11px] text-slate-500 border-b border-slate-700">
                <th className="px-6 py-2 font-medium">Наименование</th>
                <th className="px-3 py-2 font-medium text-right">Ожидалось</th>
                <th className="px-3 py-2 font-medium text-right">Получено</th>
                <th className="px-3 py-2 font-medium">Ед.</th>
                <th className="px-6 py-2 font-medium">
                  Примечание к расхождению
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {lines.map((line) => (
                <LineRow
                  key={line.id}
                  line={line}
                  isEditable={isEditable}
                  onSave={updateLine}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>

      {receipt.notes && (
        <div className="px-6 py-3 border-t border-slate-700 text-xs text-slate-400">
          Примечание: {receipt.notes}
        </div>
      )}

      {/* Confirm dialog */}
      {confirmDialog && (
        <div
          className="fixed inset-0 bg-black/60 flex items-center justify-center z-50"
          onClick={() => setConfirmDialog(false)}
        >
          <div
            className="bg-slate-800 border border-slate-600 rounded-lg p-6 w-full max-w-sm"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold text-slate-100 mb-2">
              Подтвердить приход?
            </h2>
            <p className="text-sm text-slate-400 mb-4">
              Остатки будут обновлены для{" "}
              {lines.filter((l) => l.quantity_received > 0).length} позиций.
              {hasDiscrepancy && (
                <span className="block mt-1 text-orange-400">
                  Внимание: есть расхождения с ожидаемым количеством.
                </span>
              )}
            </p>
            {error && <p className="text-red-400 text-xs mb-3">{error}</p>}
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setConfirmDialog(false)}
                className="px-4 py-1.5 text-sm text-slate-400 hover:text-slate-200"
              >
                Отмена
              </button>
              <button
                onClick={confirmReceipt}
                disabled={confirming}
                className="px-4 py-1.5 text-sm bg-green-600 hover:bg-green-500 disabled:opacity-50 text-white rounded transition-colors"
              >
                {confirming ? "Подтверждаю..." : "Подтвердить"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function LineRow({
  line,
  isEditable,
  onSave,
}: {
  line: ReceiptLine;
  isEditable: boolean;
  onSave: (lineId: string, qty: number, note: string | null) => void;
}) {
  const [qty, setQty] = useState(String(line.quantity_received));
  const [note, setNote] = useState(line.discrepancy_note ?? "");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);

  const qtyNum = parseFloat(qty) || 0;
  const hasDiscrepancy = qtyNum !== line.quantity_expected;

  async function save() {
    setSaving(true);
    await onSave(line.id, qtyNum, note || null);
    setDirty(false);
    setSaving(false);
  }

  return (
    <tr
      className={`hover:bg-slate-800/40 transition-colors ${hasDiscrepancy && qtyNum > 0 ? "bg-orange-950/20" : ""}`}
    >
      <td className="px-6 py-3 text-slate-200">{line.description}</td>
      <td className="px-3 py-3 text-right text-slate-400 font-mono">
        {line.quantity_expected.toLocaleString("ru-RU")}
      </td>
      <td className="px-3 py-3 text-right">
        {isEditable ? (
          <input
            type="number"
            min="0"
            step="any"
            value={qty}
            onChange={(e) => {
              setQty(e.target.value);
              setDirty(true);
            }}
            className={`w-24 px-2 py-1 text-sm text-right font-mono bg-slate-700 border rounded focus:outline-none focus:border-blue-500 ${
              hasDiscrepancy && qtyNum > 0
                ? "border-orange-600 text-orange-300"
                : "border-slate-600 text-slate-200"
            }`}
          />
        ) : (
          <span
            className={`font-mono ${hasDiscrepancy ? "text-orange-400" : "text-green-400"}`}
          >
            {line.quantity_received.toLocaleString("ru-RU")}
          </span>
        )}
      </td>
      <td className="px-3 py-3 text-slate-500 text-xs">{line.unit}</td>
      <td className="px-6 py-3">
        {isEditable ? (
          <div className="flex items-center gap-2">
            <input
              value={note}
              onChange={(e) => {
                setNote(e.target.value);
                setDirty(true);
              }}
              placeholder={
                hasDiscrepancy ? "Укажите причину расхождения..." : ""
              }
              className="flex-1 px-2 py-1 text-xs bg-slate-700 border border-slate-600 rounded text-slate-200 placeholder-slate-600 focus:outline-none focus:border-blue-500"
            />
            {dirty && (
              <button
                onClick={save}
                disabled={saving}
                className="px-2 py-1 text-[10px] bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded transition-colors whitespace-nowrap"
              >
                {saving ? "..." : "Сохранить"}
              </button>
            )}
          </div>
        ) : (
          <span className="text-slate-400 text-xs">
            {line.discrepancy_note ?? "—"}
          </span>
        )}
      </td>
    </tr>
  );
}
