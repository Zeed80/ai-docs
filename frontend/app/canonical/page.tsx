"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface CanonicalItem {
  id: string;
  name: string;
  category: string | null;
  unit: string | null;
  description: string | null;
  aliases: string[] | null;
  is_confirmed: boolean;
  okpd2_code: string | null;
  gost: string | null;
  created_at: string;
}

export default function CanonicalItemsPage() {
  const [items, setItems] = useState<CanonicalItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState("");
  const [confirmedOnly, setConfirmedOnly] = useState(false);
  const [showCreate, setShowCreate] = useState(false);

  const [form, setForm] = useState({
    name: "",
    category: "",
    unit: "",
    description: "",
    okpd2_code: "",
    gost: "",
  });
  const [creating, setCreating] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (q) params.set("q", q);
      if (confirmedOnly) params.set("confirmed_only", "true");
      const res = await fetch(`${API}/api/canonical?${params}`);
      if (res.ok) setItems(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    const t = setTimeout(load, q ? 300 : 0);
    return () => clearTimeout(t);
  }, [q, confirmedOnly]);

  async function create() {
    if (!form.name.trim()) return;
    setCreating(true);
    try {
      const res = await fetch(`${API}/api/canonical`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: form.name.trim(),
          category: form.category || null,
          unit: form.unit || null,
          description: form.description || null,
          okpd2_code: form.okpd2_code || null,
          gost: form.gost || null,
        }),
      });
      if (res.ok) {
        const item: CanonicalItem = await res.json();
        setItems((prev) => [item, ...prev]);
        setShowCreate(false);
        setForm({
          name: "",
          category: "",
          unit: "",
          description: "",
          okpd2_code: "",
          gost: "",
        });
      }
    } finally {
      setCreating(false);
    }
  }

  async function toggleConfirm(item: CanonicalItem) {
    const res = await fetch(`${API}/api/canonical/${item.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_confirmed: !item.is_confirmed }),
    });
    if (res.ok) {
      const updated: CanonicalItem = await res.json();
      setItems((prev) => prev.map((i) => (i.id === updated.id ? updated : i)));
    }
  }

  async function deleteItem(id: string) {
    const res = await fetch(`${API}/api/canonical/${id}`, { method: "DELETE" });
    if (res.status === 204) setItems((prev) => prev.filter((i) => i.id !== id));
  }

  const confirmed = items.filter((i) => i.is_confirmed);
  const unconfirmed = items.filter((i) => !i.is_confirmed);

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-slate-100">Canonical Items</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Нормированный справочник позиций для сравнения цен и маппинга КП
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          + Добавить
        </button>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-5 items-center">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Поиск по названию..."
          className="flex-1 px-3 py-1.5 text-sm bg-slate-800 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
        />
        <label className="flex items-center gap-1.5 text-xs text-slate-400 cursor-pointer">
          <input
            type="checkbox"
            checked={confirmedOnly}
            onChange={(e) => setConfirmedOnly(e.target.checked)}
            className="accent-blue-500"
          />
          Только подтверждённые
        </label>
        <span className="text-xs text-slate-500">{items.length} позиций</span>
      </div>

      {/* Create form */}
      {showCreate && (
        <div className="mb-5 bg-slate-800 border border-slate-700 rounded-lg p-4 space-y-3">
          <h3 className="text-sm font-semibold text-slate-200">
            Новый canonical item
          </h3>
          <div className="grid grid-cols-2 gap-3">
            <input
              autoFocus
              type="text"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="Название *"
              className="col-span-2 px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
            />
            <input
              type="text"
              value={form.category}
              onChange={(e) =>
                setForm((f) => ({ ...f, category: e.target.value }))
              }
              placeholder="Категория"
              className="px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
            />
            <input
              type="text"
              value={form.unit}
              onChange={(e) => setForm((f) => ({ ...f, unit: e.target.value }))}
              placeholder="Единица измерения (шт, кг...)"
              className="px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
            />
            <input
              type="text"
              value={form.okpd2_code}
              onChange={(e) =>
                setForm((f) => ({ ...f, okpd2_code: e.target.value }))
              }
              placeholder="ОКПД2"
              className="px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
            />
            <input
              type="text"
              value={form.gost}
              onChange={(e) => setForm((f) => ({ ...f, gost: e.target.value }))}
              placeholder="ГОСТ"
              className="px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
            />
            <input
              type="text"
              value={form.description}
              onChange={(e) =>
                setForm((f) => ({ ...f, description: e.target.value }))
              }
              placeholder="Описание"
              className="col-span-2 px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
            />
          </div>
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => setShowCreate(false)}
              className="px-3 py-1.5 text-xs text-slate-400"
            >
              Отмена
            </button>
            <button
              onClick={create}
              disabled={creating || !form.name.trim()}
              className="px-4 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
            >
              {creating ? "Создаю..." : "Создать"}
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <div className="py-12 text-center text-slate-500 text-sm">
          Загрузка...
        </div>
      ) : items.length === 0 ? (
        <div className="py-16 text-center">
          <div className="text-4xl text-slate-700 mb-3">📦</div>
          <p className="text-slate-400 text-sm">Справочник пуст.</p>
          <p className="text-slate-600 text-xs mt-1">
            Позиции добавляются при маппинге строк счетов или вручную.
          </p>
        </div>
      ) : (
        <div className="space-y-5">
          {confirmed.length > 0 && (
            <section>
              <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500 mb-2">
                Подтверждённые ({confirmed.length})
              </h2>
              <CanonicalTable
                items={confirmed}
                onToggle={toggleConfirm}
                onDelete={deleteItem}
              />
            </section>
          )}
          {unconfirmed.length > 0 && (
            <section>
              <h2 className="text-xs font-semibold uppercase tracking-wider text-amber-600 mb-2">
                Ожидают подтверждения ({unconfirmed.length})
              </h2>
              <CanonicalTable
                items={unconfirmed}
                onToggle={toggleConfirm}
                onDelete={deleteItem}
              />
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function CanonicalTable({
  items,
  onToggle,
  onDelete,
}: {
  items: CanonicalItem[];
  onToggle: (item: CanonicalItem) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="bg-slate-800 border border-slate-700 rounded-lg overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-slate-700/50 text-xs text-slate-400 uppercase">
          <tr>
            <th className="text-left px-4 py-2">Название</th>
            <th className="text-left px-3 py-2">Категория</th>
            <th className="text-left px-3 py-2">Ед.</th>
            <th className="text-left px-3 py-2">ОКПД2</th>
            <th className="text-left px-3 py-2">ГОСТ</th>
            <th className="w-20 px-3 py-2" />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-700/60">
          {items.map((item) => (
            <tr key={item.id} className="group hover:bg-slate-700/20">
              <td className="px-4 py-2.5">
                <div className="text-slate-200 font-medium">{item.name}</div>
                {item.aliases && item.aliases.length > 0 && (
                  <div className="text-xs text-slate-500 mt-0.5">
                    {item.aliases.slice(0, 3).join(", ")}
                  </div>
                )}
              </td>
              <td className="px-3 py-2.5 text-slate-400 text-xs">
                {item.category ?? "—"}
              </td>
              <td className="px-3 py-2.5 text-slate-400 text-xs">
                {item.unit ?? "—"}
              </td>
              <td className="px-3 py-2.5 font-mono text-xs text-slate-400">
                {item.okpd2_code ?? "—"}
              </td>
              <td className="px-3 py-2.5 text-xs text-slate-400">
                {item.gost ?? "—"}
              </td>
              <td className="px-3 py-2.5">
                <div className="flex items-center gap-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    onClick={() => onToggle(item)}
                    className={`text-xs px-1.5 py-0.5 rounded ${
                      item.is_confirmed
                        ? "text-slate-500 hover:text-amber-400"
                        : "text-amber-400 hover:text-green-400"
                    }`}
                    title={
                      item.is_confirmed ? "Снять подтверждение" : "Подтвердить"
                    }
                  >
                    {item.is_confirmed ? "✓" : "?"}
                  </button>
                  <button
                    onClick={() => onDelete(item.id)}
                    className="text-xs text-slate-600 hover:text-red-400"
                    title="Удалить"
                  >
                    ×
                  </button>
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
