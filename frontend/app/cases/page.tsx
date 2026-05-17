"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface CollectionItem {
  id: string;
  entity_type: string;
  entity_id: string;
  note: string | null;
  added_by: string;
  created_at: string;
}

interface Collection {
  id: string;
  name: string;
  description: string | null;
  is_closed: boolean;
  closed_at: string | null;
  closure_summary: string | null;
  items: CollectionItem[];
  created_at: string;
}

export default function CasesPage() {
  const router = useRouter();
  const [collections, setCollections] = useState<Collection[]>([]);
  const [loading, setLoading] = useState(true);
  const [showClosed, setShowClosed] = useState(false);
  const [showForm, setShowForm] = useState(false);
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [creating, setCreating] = useState(false);

  async function load(includeClosed: boolean) {
    setLoading(true);
    try {
      const res = await fetch(
        `${API}/api/collections?include_closed=${includeClosed}`,
      );
      if (res.ok) setCollections(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load(showClosed);
  }, [showClosed]);

  async function create() {
    if (!formName.trim()) return;
    setCreating(true);
    try {
      const res = await fetch(`${API}/api/collections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: formName.trim(),
          description: formDesc || null,
        }),
      });
      if (res.ok) {
        const created: Collection = await res.json();
        setShowForm(false);
        setFormName("");
        setFormDesc("");
        router.push(`/collections/${created.id}`);
      }
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-slate-100">Дела</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Рабочие подборки документов, счетов и событий
          </p>
        </div>
        <button
          onClick={() => setShowForm(true)}
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          + Новое дело
        </button>
      </div>

      {/* Filter */}
      <div className="flex gap-2 mb-4">
        {([false, true] as const).map((closed) => (
          <button
            key={String(closed)}
            onClick={() => setShowClosed(closed)}
            className={`px-3 py-1 text-xs rounded ${
              showClosed === closed
                ? "bg-slate-600 text-slate-100"
                : "bg-slate-800 text-slate-400 hover:text-slate-200"
            }`}
          >
            {closed ? "Включая закрытые" : "Активные"}
          </button>
        ))}
      </div>

      {/* Create form */}
      {showForm && (
        <div className="mb-5 bg-slate-800 border border-slate-700 rounded-lg p-4 space-y-3">
          <h3 className="text-sm font-semibold text-slate-200">Новое дело</h3>
          <input
            autoFocus
            type="text"
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
            placeholder="Название *"
            className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
          />
          <input
            type="text"
            value={formDesc}
            onChange={(e) => setFormDesc(e.target.value)}
            placeholder="Описание (необязательно)"
            className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => setShowForm(false)}
              className="px-3 py-1.5 text-xs text-slate-400"
            >
              Отмена
            </button>
            <button
              onClick={create}
              disabled={creating || !formName.trim()}
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
      ) : collections.length === 0 ? (
        <div className="py-16 text-center">
          <div className="text-4xl text-slate-700 mb-3">📂</div>
          <p className="text-slate-400 text-sm">Нет активных дел.</p>
          <p className="text-slate-600 text-xs mt-1">
            Создайте первое дело для группировки связанных документов.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {collections.map((coll) => (
            <button
              key={coll.id}
              onClick={() => router.push(`/collections/${coll.id}`)}
              className="w-full text-left flex items-start gap-4 p-4 bg-slate-800 border border-slate-700 rounded-lg hover:bg-slate-700/60 transition-colors"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-sm font-medium text-slate-200 truncate">
                    {coll.name}
                  </span>
                  {coll.is_closed && (
                    <span className="text-[10px] px-1.5 py-0.5 bg-slate-700 text-slate-500 rounded-full shrink-0">
                      закрыто
                    </span>
                  )}
                </div>
                {coll.description && (
                  <p className="text-xs text-slate-400 truncate">
                    {coll.description}
                  </p>
                )}
                <p className="text-[10px] text-slate-500 mt-1">
                  {coll.items.length} элементов · создано{" "}
                  {new Date(coll.created_at).toLocaleDateString("ru-RU")}
                </p>
              </div>
              <span
                className={`text-[10px] px-2 py-0.5 rounded-full shrink-0 mt-0.5 ${
                  coll.is_closed
                    ? "bg-slate-700 text-slate-500"
                    : "bg-blue-900/40 text-blue-300"
                }`}
              >
                {coll.is_closed ? "Закрыто" : "Активно"}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
