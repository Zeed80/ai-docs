"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useState } from "react";
import Link from "next/link";

const API = getApiBaseUrl();

interface PurchaseRequestItem {
  name: string;
  qty: number;
  unit: string;
  target_price?: number;
}

interface PurchaseRequest {
  id: string;
  title: string;
  requested_by: string;
  status: string;
  items: PurchaseRequestItem[];
  deadline?: string;
  notes?: string;
  created_at: string;
}

const STATUS_LABELS: Record<string, string> = {
  draft: "Черновик",
  approved: "Одобрена",
  rfq_sent: "КП отправлен",
  offers_received: "КП получены",
  completed: "Завершена",
  cancelled: "Отменена",
};

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-slate-700 text-slate-300",
  approved: "bg-blue-900 text-blue-200",
  rfq_sent: "bg-yellow-900 text-yellow-200",
  offers_received: "bg-indigo-900 text-indigo-200",
  completed: "bg-green-900 text-green-200",
  cancelled: "bg-red-900 text-red-300",
};

interface CreateModalProps {
  onClose: () => void;
  onCreated: () => void;
}

function CreateModal({ onClose, onCreated }: CreateModalProps) {
  const [title, setTitle] = useState("");
  const [deadline, setDeadline] = useState("");
  const [notes, setNotes] = useState("");
  const [items, setItems] = useState([
    { name: "", qty: 1, unit: "шт", target_price: "" },
  ]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  function addItem() {
    setItems([...items, { name: "", qty: 1, unit: "шт", target_price: "" }]);
  }

  function updateItem(i: number, field: string, value: string | number) {
    setItems(
      items.map((item, idx) =>
        idx === i ? { ...item, [field]: value } : item,
      ),
    );
  }

  function removeItem(i: number) {
    setItems(items.filter((_, idx) => idx !== i));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim() || items.every((it) => !it.name.trim())) {
      setError("Заполните название и хотя бы одну позицию");
      return;
    }
    setLoading(true);
    try {
      const payload = {
        title,
        deadline: deadline || null,
        notes: notes || null,
        items: items
          .filter((it) => it.name.trim())
          .map((it) => ({
            name: it.name,
            qty: Number(it.qty),
            unit: it.unit,
            target_price: it.target_price ? Number(it.target_price) : null,
          })),
      };
      const res = await fetch(`${API}/api/purchase-requests`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка создания заявки");
        return;
      }
      onCreated();
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
        className="bg-slate-800 rounded-lg p-6 w-full max-w-2xl max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-slate-100 mb-4">
          Новая заявка на закупку
        </h2>
        {error && <p className="text-red-400 text-sm mb-3">{error}</p>}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Название заявки
            </label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              placeholder="Закупка расходных материалов"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Срок подачи КП
            </label>
            <input
              type="datetime-local"
              value={deadline}
              onChange={(e) => setDeadline(e.target.value)}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs text-slate-400">Позиции</label>
              <button
                type="button"
                onClick={addItem}
                className="text-xs text-indigo-400 hover:text-indigo-300"
              >
                + добавить
              </button>
            </div>
            <div className="space-y-2">
              {items.map((item, i) => (
                <div key={i} className="grid grid-cols-12 gap-2 items-center">
                  <input
                    value={item.name}
                    onChange={(e) => updateItem(i, "name", e.target.value)}
                    placeholder="Наименование"
                    className="col-span-5 px-2 py-1.5 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
                  />
                  <input
                    type="number"
                    value={item.qty}
                    onChange={(e) => updateItem(i, "qty", e.target.value)}
                    min="0"
                    step="0.001"
                    className="col-span-2 px-2 py-1.5 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
                  />
                  <input
                    value={item.unit}
                    onChange={(e) => updateItem(i, "unit", e.target.value)}
                    placeholder="шт"
                    className="col-span-2 px-2 py-1.5 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
                  />
                  <input
                    type="number"
                    value={item.target_price}
                    onChange={(e) =>
                      updateItem(i, "target_price", e.target.value)
                    }
                    placeholder="Цена"
                    className="col-span-2 px-2 py-1.5 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
                  />
                  <button
                    type="button"
                    onClick={() => removeItem(i)}
                    className="col-span-1 text-slate-500 hover:text-red-400 text-center"
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Примечания
            </label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div className="flex gap-3 pt-2">
            <button
              type="submit"
              disabled={loading}
              className="flex-1 py-2 bg-indigo-600 text-white rounded text-sm font-medium hover:bg-indigo-500 disabled:opacity-50"
            >
              {loading ? "Создание..." : "Создать заявку"}
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

export default function ProcurementPage() {
  const [requests, setRequests] = useState<PurchaseRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [showCreate, setShowCreate] = useState(false);

  async function loadRequests() {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set("status", statusFilter);
      const res = await fetch(`${API}/api/purchase-requests?${params}`);
      const data = await res.json();
      setRequests(data.items ?? []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadRequests();
  }, [statusFilter]);

  function formatDate(d: string) {
    return new Date(d).toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-slate-100">
          Заявки на закупку
        </h1>
        <button
          onClick={() => setShowCreate(true)}
          className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-500"
        >
          + Создать заявку
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-2 mb-4">
        {[
          "",
          "draft",
          "approved",
          "rfq_sent",
          "offers_received",
          "completed",
        ].map((s) => (
          <button
            key={s}
            onClick={() => setStatusFilter(s)}
            className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
              statusFilter === s
                ? "bg-indigo-600 text-white"
                : "bg-slate-800 text-slate-400 hover:text-slate-200"
            }`}
          >
            {s === "" ? "Все" : (STATUS_LABELS[s] ?? s)}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-slate-400 text-sm">Загрузка...</p>
      ) : requests.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p className="text-4xl mb-3">📋</p>
          <p className="text-sm">Заявок на закупку нет</p>
          <button
            onClick={() => setShowCreate(true)}
            className="mt-4 px-4 py-2 bg-indigo-600 text-white rounded-md text-sm hover:bg-indigo-500"
          >
            Создать первую заявку
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {requests.map((req) => (
            <div
              key={req.id}
              className="bg-slate-800 rounded-lg p-4 hover:bg-slate-750 transition-colors"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-3 mb-1">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[req.status] ?? "bg-slate-700 text-slate-300"}`}
                    >
                      {STATUS_LABELS[req.status] ?? req.status}
                    </span>
                    <h3 className="text-sm font-medium text-slate-100 truncate">
                      {req.title}
                    </h3>
                  </div>
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                    <span>{req.items.length} позиций</span>
                    <span>Создана {formatDate(req.created_at)}</span>
                    {req.deadline && (
                      <span>Срок КП: {formatDate(req.deadline)}</span>
                    )}
                    <span>Автор: {req.requested_by}</span>
                  </div>
                  {req.items.length > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {req.items.slice(0, 3).map((item, i) => (
                        <span
                          key={i}
                          className="px-2 py-0.5 bg-slate-700 rounded text-xs text-slate-300"
                        >
                          {item.name} — {item.qty} {item.unit}
                        </span>
                      ))}
                      {req.items.length > 3 && (
                        <span className="px-2 py-0.5 bg-slate-700 rounded text-xs text-slate-400">
                          +{req.items.length - 3} ещё
                        </span>
                      )}
                    </div>
                  )}
                </div>
                <div className="flex gap-2 shrink-0">
                  {req.status === "draft" && (
                    <Link
                      href={`/procurement/${req.id}`}
                      className="px-3 py-1.5 text-xs bg-slate-700 text-slate-300 rounded hover:bg-slate-600"
                    >
                      Открыть
                    </Link>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {showCreate && (
        <CreateModal
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            loadRequests();
          }}
        />
      )}
    </div>
  );
}
