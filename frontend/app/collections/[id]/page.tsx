"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

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

interface TimelineEvent {
  timestamp: string;
  event_type: string;
  entity_type: string;
  entity_id: string;
  summary: string;
}

const ENTITY_TYPE_LABELS: Record<string, string> = {
  invoice: "Счёт",
  document: "Документ",
  anomaly: "Аномалия",
  supplier: "Поставщик",
  approval: "Согласование",
};

export default function CollectionDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const [coll, setColl] = useState<Collection | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"items" | "timeline">("items");

  const [summarizing, setSummarizing] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  const [closing, setClosing] = useState(false);
  const [confirmClose, setConfirmClose] = useState(false);

  const [addType, setAddType] = useState("invoice");
  const [addId, setAddId] = useState("");
  const [addNote, setAddNote] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<
    { entity_type: string; entity_id: string; title: string; reason: string }[]
  >([]);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const [itemSearch, setItemSearch] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [collRes, tlRes] = await Promise.all([
        fetch(`${API}/api/collections/${id}`),
        fetch(`${API}/api/collections/${id}/timeline`),
      ]);
      if (collRes.ok) setColl(await collRes.json());
      if (tlRes.ok) {
        const tlData = await tlRes.json();
        setTimeline(tlData.events ?? []);
      }
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function loadSuggestions() {
    setLoadingSuggestions(true);
    try {
      const res = await fetch(`${API}/api/collections/${id}/suggest`);
      if (res.ok) {
        const data = await res.json();
        setSuggestions(data.suggestions ?? []);
      }
    } finally {
      setLoadingSuggestions(false);
    }
  }

  async function addSuggestion(s: { entity_type: string; entity_id: string }) {
    await fetch(`${API}/api/collections/${id}/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entity_type: s.entity_type,
        entity_id: s.entity_id,
      }),
    });
    setSuggestions((prev) => prev.filter((x) => x.entity_id !== s.entity_id));
    await load();
  }

  async function addItem() {
    if (!addId.trim()) return;
    setAdding(true);
    setAddError(null);
    try {
      const res = await fetch(`${API}/api/collections/${id}/items`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          entity_type: addType,
          entity_id: addId.trim(),
          note: addNote.trim() || null,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setAddError(err.detail ?? "Ошибка добавления");
        return;
      }
      const updated: Collection = await res.json();
      setColl(updated);
      setAddId("");
      setAddNote("");
    } finally {
      setAdding(false);
    }
  }

  async function removeItem(itemId: string) {
    const res = await fetch(`${API}/api/collections/${id}/items/${itemId}`, {
      method: "DELETE",
    });
    if (res.ok)
      setColl((prev) =>
        prev
          ? { ...prev, items: prev.items.filter((i) => i.id !== itemId) }
          : prev,
      );
  }

  async function getSummary() {
    setSummarizing(true);
    try {
      const res = await fetch(`${API}/api/collections/${id}/summarize`, {
        method: "POST",
      });
      if (res.ok) {
        const data = await res.json();
        setSummary(data.summary);
      }
    } finally {
      setSummarizing(false);
    }
  }

  async function closeCollection() {
    setClosing(true);
    try {
      const res = await fetch(`${API}/api/collections/${id}/close`, {
        method: "POST",
      });
      if (res.ok) {
        const updated: Collection = await res.json();
        setColl(updated);
        setSummary(updated.closure_summary);
        setConfirmClose(false);
      }
    } finally {
      setClosing(false);
    }
  }

  if (loading)
    return <div className="p-6 text-slate-400 text-sm">Загрузка...</div>;
  if (!coll)
    return (
      <div className="p-6 text-slate-400 text-sm">Подборка не найдена</div>
    );

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <button
        onClick={() => router.push("/collections")}
        className="text-sm text-slate-500 hover:text-slate-300 mb-4 block"
      >
        ← Все подборки
      </button>

      {/* Header */}
      <div className="flex items-start justify-between mb-5">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-xl font-bold text-slate-100">{coll.name}</h1>
            {coll.is_closed && (
              <span className="text-xs px-2 py-0.5 bg-slate-700 text-slate-400 rounded-full">
                закрыта
              </span>
            )}
          </div>
          {coll.description && (
            <p className="text-sm text-slate-500 mt-1">{coll.description}</p>
          )}
          <p className="text-xs text-slate-600 mt-1">
            {coll.items.length} элементов · создана{" "}
            {new Date(coll.created_at).toLocaleDateString("ru-RU")}
          </p>
        </div>

        {!coll.is_closed && (
          <div className="flex gap-2">
            <button
              onClick={getSummary}
              disabled={summarizing || coll.items.length === 0}
              className="px-3 py-1.5 text-xs bg-purple-700 text-white rounded hover:bg-purple-600 disabled:opacity-40"
            >
              {summarizing ? "Анализирую…" : "AI-резюме"}
            </button>
            {confirmClose ? (
              <>
                <span className="text-xs text-amber-400 self-center">
                  Закрыть?
                </span>
                <button
                  onClick={closeCollection}
                  disabled={closing}
                  className="px-3 py-1.5 text-xs bg-amber-600 text-white rounded hover:bg-amber-500 disabled:opacity-50"
                >
                  {closing ? "…" : "Да"}
                </button>
                <button
                  onClick={() => setConfirmClose(false)}
                  className="px-3 py-1.5 text-xs text-slate-400"
                >
                  Отмена
                </button>
              </>
            ) : (
              <button
                onClick={() => setConfirmClose(true)}
                className="px-3 py-1.5 text-xs border border-slate-600 text-slate-400 hover:text-slate-200 rounded"
              >
                Закрыть подборку
              </button>
            )}
          </div>
        )}
      </div>

      {/* Closure summary */}
      {(summary ?? coll.closure_summary) && (
        <div className="mb-5 px-4 py-3 bg-purple-900/20 border border-purple-700/40 rounded-lg">
          <p className="text-xs font-semibold text-purple-400 mb-1">
            AI-резюме
          </p>
          <p className="text-sm text-slate-300 whitespace-pre-wrap">
            {summary ?? coll.closure_summary}
          </p>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 border-b border-slate-700 mb-4">
        {(["items", "timeline"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
              tab === t
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-slate-500 hover:text-slate-300"
            }`}
          >
            {t === "items"
              ? `Элементы (${coll.items.length})`
              : `История (${timeline.length})`}
          </button>
        ))}
      </div>

      {/* Items tab */}
      {tab === "items" && (
        <div className="space-y-3">
          {!coll.is_closed && (
            <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
              <h3 className="text-xs font-semibold text-slate-400 mb-3 uppercase tracking-wider">
                Добавить элемент
              </h3>
              <div className="flex gap-2 flex-wrap">
                <select
                  value={addType}
                  onChange={(e) => setAddType(e.target.value)}
                  className="px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 rounded outline-none focus:border-blue-400"
                >
                  {Object.entries(ENTITY_TYPE_LABELS).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
                <input
                  type="text"
                  value={addId}
                  onChange={(e) => setAddId(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && addItem()}
                  placeholder="UUID элемента..."
                  className="flex-1 min-w-40 px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400 font-mono"
                />
                <input
                  type="text"
                  value={addNote}
                  onChange={(e) => setAddNote(e.target.value)}
                  placeholder="Заметка..."
                  className="flex-1 min-w-32 px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
                />
                <button
                  onClick={addItem}
                  disabled={adding || !addId.trim()}
                  className="px-4 py-1.5 text-sm bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                >
                  {adding ? "…" : "Добавить"}
                </button>
              </div>
              {addError && (
                <p className="text-xs text-red-400 mt-2">{addError}</p>
              )}
              <div className="mt-2 pt-2 border-t border-slate-700/60">
                <button
                  onClick={loadSuggestions}
                  disabled={loadingSuggestions}
                  className="text-xs text-blue-400 hover:text-blue-300 disabled:opacity-50"
                >
                  {loadingSuggestions
                    ? "Подбираю..."
                    : "Подобрать автоматически ↗"}
                </button>
                {suggestions.length > 0 && (
                  <div className="mt-2 space-y-1">
                    {suggestions.map((s) => (
                      <div
                        key={s.entity_id}
                        className="flex items-center gap-2 text-xs bg-slate-700/50 rounded px-2 py-1.5"
                      >
                        <span className="text-[10px] px-1.5 py-0.5 bg-slate-600 text-slate-300 rounded-full shrink-0">
                          {ENTITY_TYPE_LABELS[s.entity_type] ?? s.entity_type}
                        </span>
                        <span className="flex-1 text-slate-300 truncate">
                          {s.title}
                        </span>
                        <span className="text-slate-500 italic truncate max-w-[120px]">
                          {s.reason}
                        </span>
                        <button
                          onClick={() => addSuggestion(s)}
                          className="shrink-0 px-2 py-0.5 text-[10px] bg-blue-600 text-white rounded hover:bg-blue-700"
                        >
                          + Добавить
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {coll.items.length > 3 && (
            <input
              type="text"
              value={itemSearch}
              onChange={(e) => setItemSearch(e.target.value)}
              placeholder="Поиск по элементам..."
              className="w-full px-3 py-1.5 text-sm bg-slate-800 border border-slate-700 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400 mb-2"
            />
          )}

          {coll.items.length === 0 ? (
            <div className="py-10 text-center text-slate-500 text-sm">
              Подборка пуста
            </div>
          ) : (
            <div className="space-y-2">
              {coll.items
                .filter(
                  (item) =>
                    !itemSearch ||
                    item.entity_id
                      .toLowerCase()
                      .includes(itemSearch.toLowerCase()) ||
                    (item.note ?? "")
                      .toLowerCase()
                      .includes(itemSearch.toLowerCase()) ||
                    item.entity_type
                      .toLowerCase()
                      .includes(itemSearch.toLowerCase()),
                )
                .map((item) => (
                  <div
                    key={item.id}
                    className="flex items-center gap-3 bg-slate-800 border border-slate-700 rounded-lg px-4 py-3"
                  >
                    <span className="text-[10px] px-1.5 py-0.5 bg-slate-700 text-slate-400 rounded-full shrink-0">
                      {ENTITY_TYPE_LABELS[item.entity_type] ?? item.entity_type}
                    </span>
                    <span className="font-mono text-xs text-slate-400 flex-1 truncate">
                      {item.entity_id}
                    </span>
                    {item.note && (
                      <span className="text-xs text-slate-500 truncate max-w-xs">
                        {item.note}
                      </span>
                    )}
                    <span className="text-xs text-slate-600 shrink-0">
                      {new Date(item.created_at).toLocaleDateString("ru-RU")}
                    </span>
                    {!coll.is_closed && (
                      <button
                        onClick={() => removeItem(item.id)}
                        className="text-slate-600 hover:text-red-400 text-sm ml-1"
                        title="Удалить"
                      >
                        ×
                      </button>
                    )}
                  </div>
                ))}
            </div>
          )}
        </div>
      )}

      {/* Timeline tab */}
      {tab === "timeline" && (
        <div className="space-y-2">
          {timeline.length === 0 ? (
            <div className="py-10 text-center text-slate-500 text-sm">
              История пуста
            </div>
          ) : (
            timeline.map((ev, i) => (
              <div key={i} className="flex gap-3 items-start">
                <div className="flex flex-col items-center shrink-0 mt-1">
                  <div className="w-2 h-2 rounded-full bg-blue-500" />
                  {i < timeline.length - 1 && (
                    <div
                      className="w-px flex-1 bg-slate-700 mt-1"
                      style={{ minHeight: 24 }}
                    />
                  )}
                </div>
                <div className="pb-3 flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-[10px] text-slate-500">
                      {new Date(ev.timestamp).toLocaleString("ru-RU")}
                    </span>
                    <span className="text-[10px] px-1 bg-slate-700 text-slate-400 rounded">
                      {ev.event_type}
                    </span>
                    <span className="text-[10px] text-slate-600">
                      {ENTITY_TYPE_LABELS[ev.entity_type] ?? ev.entity_type}
                    </span>
                  </div>
                  <p className="text-sm text-slate-300">{ev.summary}</p>
                </div>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
