"use client";

import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface QuarantineEntry {
  id: string;
  document_id: string;
  reason: string;
  original_filename: string;
  detected_mime: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  decision: string | null;
  created_at: string;
}

interface AllowlistEntry {
  id: string;
  extension: string;
  is_allowed: boolean;
  added_by: string;
}

const REASON_LABELS: Record<string, string> = {
  extension_not_allowed: "Расширение не разрешено",
  mime_mismatch: "Тип файла не совпадает",
  size_limit_exceeded: "Превышен размер файла",
  hash_collision: "Коллизия хеша",
};

export default function QuarantinePage() {
  const [entries, setEntries] = useState<QuarantineEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [allowlist, setAllowlist] = useState<AllowlistEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [newExt, setNewExt] = useState("");
  const [tab, setTab] = useState<"queue" | "allowlist">("queue");
  const [actionId, setActionId] = useState<string | null>(null);

  async function loadData() {
    setLoading(true);
    try {
      const [qRes, aRes] = await Promise.all([
        fetch(`${API}/api/quarantine?pending_only=true&limit=100`),
        fetch(`${API}/api/quarantine/allowlist`),
      ]);
      const qData = await qRes.json();
      const aData = await aRes.json();
      setEntries(qData.items ?? []);
      setTotal(qData.total ?? 0);
      setAllowlist(Array.isArray(aData) ? aData : []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
  }, []);

  async function release(id: string) {
    setActionId(id);
    try {
      await fetch(`${API}/api/quarantine/${id}/release`, { method: "POST" });
      await loadData();
    } finally {
      setActionId(null);
    }
  }

  async function deleteEntry(id: string) {
    if (
      !confirm(
        "Удалить файл из карантина? Файл будет безвозвратно удалён из хранилища.",
      )
    )
      return;
    setActionId(id);
    try {
      await fetch(`${API}/api/quarantine/${id}`, { method: "DELETE" });
      await loadData();
    } finally {
      setActionId(null);
    }
  }

  async function addToAllowlist() {
    const ext = newExt.trim();
    if (!ext) return;
    await fetch(`${API}/api/quarantine/allowlist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extension: ext, is_allowed: true }),
    });
    setNewExt("");
    await loadData();
  }

  async function removeFromAllowlist(id: string) {
    await fetch(`${API}/api/quarantine/allowlist/${id}`, { method: "DELETE" });
    await loadData();
  }

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-semibold text-slate-100">
            Карантин файлов
          </h1>
          <p className="text-sm text-slate-400 mt-0.5">
            Файлы с неразрешёнными расширениями ожидают проверки
          </p>
        </div>
        {total > 0 && (
          <span className="text-sm font-bold bg-red-500 text-white rounded-full px-3 py-1">
            {total} ожидают
          </span>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-slate-700">
        {(["queue", "allowlist"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
              tab === t
                ? "border-blue-500 text-blue-400"
                : "border-transparent text-slate-400 hover:text-slate-200"
            }`}
          >
            {t === "queue" ? `Очередь (${total})` : "Разрешённые расширения"}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-slate-400">Загрузка...</p>
      ) : tab === "queue" ? (
        entries.length === 0 ? (
          <div className="text-center py-16 text-slate-500">
            <svg
              className="w-12 h-12 mx-auto mb-3 opacity-50"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.5}
                d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
              />
            </svg>
            <p className="font-medium">Карантин пуст</p>
            <p className="text-sm mt-1">
              Все загруженные файлы прошли проверку
            </p>
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-slate-700">
            <table className="w-full text-sm">
              <thead className="bg-slate-800 text-slate-400 text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-4 py-3 text-left">Файл</th>
                  <th className="px-4 py-3 text-left">Причина</th>
                  <th className="px-4 py-3 text-left">MIME</th>
                  <th className="px-4 py-3 text-left">Дата</th>
                  <th className="px-4 py-3 text-right">Действия</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700">
                {entries.map((entry) => (
                  <tr
                    key={entry.id}
                    className="bg-slate-900 hover:bg-slate-800/50"
                  >
                    <td className="px-4 py-3 font-mono text-slate-200 max-w-[240px] truncate">
                      {entry.original_filename}
                    </td>
                    <td className="px-4 py-3">
                      <span className="px-2 py-0.5 rounded text-xs bg-red-900/50 text-red-300 border border-red-800/50">
                        {REASON_LABELS[entry.reason] ?? entry.reason}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-slate-400 text-xs font-mono">
                      {entry.detected_mime ?? "—"}
                    </td>
                    <td className="px-4 py-3 text-slate-400 text-xs">
                      {new Date(entry.created_at).toLocaleString("ru-RU")}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => release(entry.id)}
                          disabled={actionId === entry.id}
                          className="px-3 py-1.5 text-xs font-medium bg-green-700 hover:bg-green-600 text-white rounded transition-colors disabled:opacity-50"
                        >
                          Разрешить
                        </button>
                        <button
                          onClick={() => deleteEntry(entry.id)}
                          disabled={actionId === entry.id}
                          className="px-3 py-1.5 text-xs font-medium bg-red-700 hover:bg-red-600 text-white rounded transition-colors disabled:opacity-50"
                        >
                          Удалить
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : (
        <div>
          <div className="flex gap-2 mb-4">
            <input
              value={newExt}
              onChange={(e) => setNewExt(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addToAllowlist()}
              placeholder=".pdf, .xlsx, ..."
              className="flex-1 max-w-xs px-3 py-2 text-sm bg-slate-800 border border-slate-600 rounded text-slate-200 placeholder-slate-500 focus:outline-none focus:border-blue-500"
            />
            <button
              onClick={addToAllowlist}
              className="px-4 py-2 text-sm font-medium bg-blue-600 hover:bg-blue-500 text-white rounded transition-colors"
            >
              Добавить
            </button>
          </div>
          <div className="overflow-hidden rounded-lg border border-slate-700">
            <table className="w-full text-sm">
              <thead className="bg-slate-800 text-slate-400 text-xs uppercase tracking-wide">
                <tr>
                  <th className="px-4 py-3 text-left">Расширение</th>
                  <th className="px-4 py-3 text-left">Добавлено</th>
                  <th className="px-4 py-3 text-right">Действия</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-700">
                {allowlist.map((entry) => (
                  <tr
                    key={entry.id}
                    className="bg-slate-900 hover:bg-slate-800/50"
                  >
                    <td className="px-4 py-3 font-mono text-slate-200">
                      {entry.extension}
                    </td>
                    <td className="px-4 py-3 text-slate-400">
                      {entry.added_by}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => removeFromAllowlist(entry.id)}
                        className="px-3 py-1.5 text-xs font-medium bg-slate-700 hover:bg-red-700 text-slate-300 hover:text-white rounded transition-colors"
                      >
                        Удалить
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
