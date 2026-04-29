"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface CanonicalItem {
  id: string;
  name: string;
  unit?: string;
  category?: string;
  okpd2_code?: string;
  gost?: string;
  hazard_class?: string;
}

interface NormCard {
  id: string;
  canonical_item_id: string;
  product_code?: string;
  norm_qty: number;
  unit: string;
  loss_factor: number;
  valid_from?: string;
  valid_to?: string;
  approved_by?: string;
  notes?: string;
  created_at: string;
}

interface CreateNormCardFormProps {
  items: CanonicalItem[];
  onCreated: () => void;
  onClose: () => void;
}

function CreateNormCardForm({
  items,
  onCreated,
  onClose,
}: CreateNormCardFormProps) {
  const [canonicalItemId, setCanonicalItemId] = useState("");
  const [normQty, setNormQty] = useState("1");
  const [unit, setUnit] = useState("шт");
  const [productCode, setProductCode] = useState("");
  const [lossFactor, setLossFactor] = useState("1.0");
  const [approvedBy, setApprovedBy] = useState("");
  const [notes, setNotes] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canonicalItemId || !normQty || !unit) {
      setError("Заполните обязательные поля");
      return;
    }
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/normalization/norm-cards`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          canonical_item_id: canonicalItemId,
          norm_qty: Number(normQty),
          unit,
          product_code: productCode || null,
          loss_factor: Number(lossFactor),
          approved_by: approvedBy || null,
          notes: notes || null,
        }),
      });
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка");
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
        className="bg-slate-800 rounded-lg p-6 w-full max-w-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-slate-100 mb-4">
          Новая нормкарточка
        </h2>
        {error && <p className="text-red-400 text-sm mb-3">{error}</p>}
        <form onSubmit={handleSubmit} className="space-y-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Позиция каталога *
            </label>
            <select
              value={canonicalItemId}
              onChange={(e) => setCanonicalItemId(e.target.value)}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            >
              <option value="">— выберите позицию —</option>
              {items.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.name} {i.unit ? `(${i.unit})` : ""}
                </option>
              ))}
            </select>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Норма расхода *
              </label>
              <input
                type="number"
                value={normQty}
                onChange={(e) => setNormQty(e.target.value)}
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
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Коэф. потерь
              </label>
              <input
                type="number"
                value={lossFactor}
                onChange={(e) => setLossFactor(e.target.value)}
                step="0.001"
                min="1"
                className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Код изделия
              </label>
              <input
                value={productCode}
                onChange={(e) => setProductCode(e.target.value)}
                placeholder="АЛ-2025-001"
                className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              />
            </div>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Утверждено
            </label>
            <input
              value={approvedBy}
              onChange={(e) => setApprovedBy(e.target.value)}
              placeholder="Иванов И.И."
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Примечание
            </label>
            <textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={2}
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div className="flex gap-3 pt-1">
            <button
              type="submit"
              disabled={loading}
              className="flex-1 py-2 bg-indigo-600 text-white rounded text-sm font-medium hover:bg-indigo-500 disabled:opacity-50"
            >
              {loading ? "Создание..." : "Создать"}
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

interface ClassificationModalProps {
  item: CanonicalItem;
  onClose: () => void;
  onSaved: () => void;
}

function ClassificationModal({
  item,
  onClose,
  onSaved,
}: ClassificationModalProps) {
  const [okpd2, setOkpd2] = useState(item.okpd2_code ?? "");
  const [gost, setGost] = useState(item.gost ?? "");
  const [hazard, setHazard] = useState(item.hazard_class ?? "");
  const [loading, setLoading] = useState(false);

  async function handleSave() {
    setLoading(true);
    try {
      await fetch(`${API}/api/normalization/canonical-items/${item.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          okpd2_code: okpd2 || null,
          gost: gost || null,
          hazard_class: hazard || null,
        }),
      });
      onSaved();
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
        <h2 className="text-base font-semibold text-slate-100 mb-1">
          Классификация
        </h2>
        <p className="text-xs text-slate-400 mb-4 truncate">{item.name}</p>
        <div className="space-y-3">
          <div>
            <label className="block text-xs text-slate-400 mb-1">ОКПД2</label>
            <input
              value={okpd2}
              onChange={(e) => setOkpd2(e.target.value)}
              placeholder="26.20.11.110"
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              ГОСТ / ТУ
            </label>
            <input
              value={gost}
              onChange={(e) => setGost(e.target.value)}
              placeholder="ГОСТ 15150-69"
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Класс опасности
            </label>
            <input
              value={hazard}
              onChange={(e) => setHazard(e.target.value)}
              placeholder="3"
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
        </div>
        <div className="flex gap-3 mt-4">
          <button
            onClick={handleSave}
            disabled={loading}
            className="flex-1 py-2 bg-indigo-600 text-white rounded text-sm font-medium hover:bg-indigo-500 disabled:opacity-50"
          >
            {loading ? "Сохранение..." : "Сохранить"}
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 text-slate-400 hover:text-slate-200 text-sm"
          >
            Отмена
          </button>
        </div>
      </div>
    </div>
  );
}

export default function NormCardsPage() {
  const [tab, setTab] = useState<"cards" | "items">("cards");
  const [cards, setCards] = useState<NormCard[]>([]);
  const [items, setItems] = useState<CanonicalItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [editingItem, setEditingItem] = useState<CanonicalItem | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 3000);
  }

  async function loadCards() {
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/normalization/norm-cards?limit=100`);
      const data = await res.json();
      setCards(data.items ?? []);
    } finally {
      setLoading(false);
    }
  }

  async function loadItems() {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (search) params.set("search", search);
      const res = await fetch(
        `${API}/api/normalization/canonical-items?${params}`,
      ).catch(() => fetch(`${API}/api/search/canonical-items?${params}`));
      if (res.ok) {
        const data = await res.json();
        setItems(data.items ?? data ?? []);
      }
    } finally {
      setLoading(false);
    }
  }

  async function loadAllItems() {
    try {
      const res = await fetch(
        `${API}/api/normalization/canonical-items?limit=200`,
      ).catch(() => fetch(`${API}/api/search/canonical-items?limit=200`));
      if (res.ok) {
        const data = await res.json();
        setItems(data.items ?? data ?? []);
      }
    } catch {}
  }

  useEffect(() => {
    if (tab === "cards") {
      loadCards();
      loadAllItems();
    } else {
      loadItems();
    }
  }, [tab, search]);

  async function deleteCard(id: string) {
    await fetch(`${API}/api/normalization/norm-cards/${id}`, {
      method: "DELETE",
    });
    showToast("Нормкарточка удалена");
    loadCards();
  }

  const itemById = Object.fromEntries(items.map((i) => [i.id, i]));

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-slate-100">Нормирование</h1>
        {tab === "cards" && (
          <button
            onClick={() => setShowCreate(true)}
            className="px-4 py-2 bg-indigo-600 text-white rounded-md text-sm font-medium hover:bg-indigo-500"
          >
            + Нормкарточка
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 bg-slate-800 rounded-lg p-1 w-fit">
        {(
          [
            { key: "cards", label: "Нормкарточки" },
            { key: "items", label: "Каталог (ОКПД2/ГОСТ)" },
          ] as const
        ).map(({ key, label }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
              tab === key
                ? "bg-indigo-600 text-white"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* NormCards tab */}
      {tab === "cards" &&
        (loading ? (
          <p className="text-slate-400 text-sm">Загрузка...</p>
        ) : cards.length === 0 ? (
          <div className="text-center py-16 text-slate-500">
            <p className="text-4xl mb-3">📐</p>
            <p className="text-sm">Нормкарточек нет</p>
            <button
              onClick={() => setShowCreate(true)}
              className="mt-4 px-4 py-2 bg-indigo-600 text-white rounded-md text-sm hover:bg-indigo-500"
            >
              Создать нормкарточку
            </button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-slate-700">
                  {[
                    "Позиция",
                    "Код изделия",
                    "Норма",
                    "Ед.",
                    "Коэф. потерь",
                    "Утверждено",
                    "",
                  ].map((h) => (
                    <th
                      key={h}
                      className="text-left py-2 pr-4 text-xs font-medium text-slate-500 uppercase tracking-wider"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {cards.map((card) => {
                  const item = itemById[card.canonical_item_id];
                  return (
                    <tr key={card.id} className="hover:bg-slate-800/50">
                      <td className="py-3 pr-4 text-slate-200 max-w-xs truncate">
                        {item?.name ?? (
                          <span className="text-slate-500 text-xs">
                            {card.canonical_item_id.slice(0, 8)}…
                          </span>
                        )}
                      </td>
                      <td className="py-3 pr-4 text-slate-400 text-xs font-mono">
                        {card.product_code ?? "—"}
                      </td>
                      <td className="py-3 pr-4 font-medium text-slate-100">
                        {card.norm_qty}
                      </td>
                      <td className="py-3 pr-4 text-slate-400">{card.unit}</td>
                      <td className="py-3 pr-4 text-slate-400">
                        {card.loss_factor !== 1 ? (
                          <span className="text-yellow-400">
                            {card.loss_factor}×
                          </span>
                        ) : (
                          "1.0×"
                        )}
                      </td>
                      <td className="py-3 pr-4 text-slate-500 text-xs">
                        {card.approved_by ?? "—"}
                      </td>
                      <td className="py-3">
                        <button
                          onClick={() => deleteCard(card.id)}
                          className="text-xs text-slate-600 hover:text-red-400 transition-colors"
                        >
                          удалить
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))}

      {/* Canonical items tab */}
      {tab === "items" && (
        <div>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Поиск позиции..."
            className="w-full mb-4 px-3 py-2 bg-slate-800 rounded-lg text-sm text-slate-100 border border-slate-700 focus:outline-none focus:border-indigo-500"
          />
          {loading ? (
            <p className="text-slate-400 text-sm">Загрузка...</p>
          ) : items.length === 0 ? (
            <p className="text-slate-500 text-sm text-center py-12">
              Позиций не найдено
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-700">
                    {[
                      "Наименование",
                      "Ед.",
                      "ОКПД2",
                      "ГОСТ/ТУ",
                      "Опасность",
                      "",
                    ].map((h) => (
                      <th
                        key={h}
                        className="text-left py-2 pr-4 text-xs font-medium text-slate-500 uppercase tracking-wider"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800">
                  {items.map((item) => (
                    <tr key={item.id} className="hover:bg-slate-800/50">
                      <td className="py-3 pr-4 text-slate-200 max-w-xs truncate">
                        {item.name}
                      </td>
                      <td className="py-3 pr-4 text-slate-400">
                        {item.unit ?? "—"}
                      </td>
                      <td className="py-3 pr-4 text-slate-300 text-xs font-mono">
                        {item.okpd2_code ?? (
                          <span className="text-slate-600">—</span>
                        )}
                      </td>
                      <td className="py-3 pr-4 text-slate-400 text-xs">
                        {item.gost ?? <span className="text-slate-600">—</span>}
                      </td>
                      <td className="py-3 pr-4">
                        {item.hazard_class ? (
                          <span className="px-2 py-0.5 bg-orange-900 text-orange-200 rounded text-xs">
                            {item.hazard_class} кл.
                          </span>
                        ) : (
                          <span className="text-slate-600">—</span>
                        )}
                      </td>
                      <td className="py-3">
                        <button
                          onClick={() => setEditingItem(item)}
                          className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
                        >
                          редактировать
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {showCreate && (
        <CreateNormCardForm
          items={items}
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            loadCards();
            showToast("Нормкарточка создана");
          }}
        />
      )}

      {editingItem && (
        <ClassificationModal
          item={editingItem}
          onClose={() => setEditingItem(null)}
          onSaved={() => {
            setEditingItem(null);
            loadItems();
            showToast("Классификация обновлена");
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
