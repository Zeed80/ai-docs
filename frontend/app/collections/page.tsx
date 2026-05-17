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

export default function CollectionsPage() {
  const router = useRouter();
  const [collections, setCollections] = useState<Collection[]>([]);
  const [loading, setLoading] = useState(true);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [creating, setCreating] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const res = await fetch(`${API}/api/collections`);
      if (res.ok) setCollections(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function create() {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const res = await fetch(`${API}/api/collections`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: newName.trim(),
          description: newDesc.trim() || null,
        }),
      });
      if (res.ok) {
        const coll: Collection = await res.json();
        setCollections((prev) => [coll, ...prev]);
        setShowCreate(false);
        setNewName("");
        setNewDesc("");
      }
    } finally {
      setCreating(false);
    }
  }

  const open = collections.filter((c) => !c.is_closed);
  const closed = collections.filter((c) => c.is_closed);

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-slate-100">Подборки</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Группировка документов, счетов и событий для совместного анализа
          </p>
        </div>
        <button
          onClick={() => setShowCreate(true)}
          className="px-3 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700"
        >
          + Новая подборка
        </button>
      </div>

      {showCreate && (
        <div className="mb-6 bg-slate-800 border border-slate-700 rounded-lg p-4 space-y-3">
          <h3 className="text-sm font-semibold text-slate-200">
            Создать подборку
          </h3>
          <input
            autoFocus
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && create()}
            placeholder="Название..."
            className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
          />
          <input
            type="text"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder="Описание (необязательно)..."
            className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
          />
          <div className="flex gap-2 justify-end">
            <button
              onClick={() => {
                setShowCreate(false);
                setNewName("");
                setNewDesc("");
              }}
              className="px-3 py-1.5 text-xs text-slate-400"
            >
              Отмена
            </button>
            <button
              onClick={create}
              disabled={creating || !newName.trim()}
              className="px-3 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
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
          <div className="text-slate-600 text-4xl mb-3">📂</div>
          <p className="text-slate-400 text-sm">Подборок пока нет.</p>
          <p className="text-slate-600 text-xs mt-1">
            Создайте подборку для группировки связанных документов и счетов.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {open.length > 0 && (
            <section>
              <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500 mb-2">
                Активные ({open.length})
              </h2>
              <div className="grid gap-3">
                {open.map((c) => (
                  <CollectionCard
                    key={c.id}
                    coll={c}
                    onClick={() => router.push(`/collections/${c.id}`)}
                  />
                ))}
              </div>
            </section>
          )}
          {closed.length > 0 && (
            <section>
              <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500 mb-2">
                Закрытые ({closed.length})
              </h2>
              <div className="grid gap-3">
                {closed.map((c) => (
                  <CollectionCard
                    key={c.id}
                    coll={c}
                    onClick={() => router.push(`/collections/${c.id}`)}
                  />
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function CollectionCard({
  coll,
  onClick,
}: {
  coll: Collection;
  onClick: () => void;
}) {
  const entityCounts: Record<string, number> = {};
  for (const item of coll.items) {
    entityCounts[item.entity_type] = (entityCounts[item.entity_type] ?? 0) + 1;
  }
  return (
    <button
      onClick={onClick}
      className="w-full text-left bg-slate-800 border border-slate-700 rounded-lg p-4 hover:border-slate-600 transition-colors"
    >
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-slate-100">
              {coll.name}
            </span>
            {coll.is_closed && (
              <span className="text-[10px] px-1.5 py-0.5 bg-slate-700 text-slate-400 rounded-full">
                закрыта
              </span>
            )}
          </div>
          {coll.description && (
            <p className="text-xs text-slate-500 mt-0.5 truncate">
              {coll.description}
            </p>
          )}
          {coll.closure_summary && (
            <p className="text-xs text-purple-400 mt-1 line-clamp-2">
              {coll.closure_summary}
            </p>
          )}
        </div>
        <div className="flex items-center gap-3 ml-4 shrink-0 text-xs text-slate-500">
          {Object.entries(entityCounts).map(([type, count]) => (
            <span key={type}>
              {count} {type}
            </span>
          ))}
          <span className="text-slate-600">
            {new Date(coll.created_at).toLocaleDateString("ru-RU")}
          </span>
        </div>
      </div>
    </button>
  );
}
