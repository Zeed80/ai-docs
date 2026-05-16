"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

interface ApiKeyOut {
  id: string;
  name: string;
  user_sub: string;
  scopes: string[];
  is_active: boolean;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

const ALL_SCOPES = [
  "document.read",
  "invoice.read",
  "invoice.export",
  "supplier.read",
  "email.read",
];

function ApiKeysContent() {
  const [keys, setKeys] = useState<ApiKeyOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [newName, setNewName] = useState("");
  const [newScopes, setNewScopes] = useState<string[]>([]);
  const [createdKey, setCreatedKey] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  function load() {
    setLoading(true);
    fetch(`${API}/api/admin/api-keys`, { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => setKeys(d.items))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, []);

  async function create() {
    if (!newName.trim()) {
      alert("Введите название");
      return;
    }
    setCreating(true);
    try {
      const res = await fetch(`${API}/api/admin/api-keys`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ name: newName.trim(), scopes: newScopes }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCreatedKey(data.raw_key);
      setNewName("");
      setNewScopes([]);
      setShowForm(false);
      load();
    } catch (e: unknown) {
      alert(`Ошибка: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setCreating(false);
    }
  }

  async function revoke(id: string) {
    if (!confirm("Отозвать API-ключ?")) return;
    await fetch(`${API}/api/admin/api-keys/${id}`, {
      method: "DELETE",
      credentials: "include",
      headers: csrfHeaders(),
    });
    load();
  }

  function toggleScope(s: string) {
    setNewScopes((prev) =>
      prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s],
    );
  }

  return (
    <div className="space-y-4">
      {createdKey && (
        <div className="rounded-lg border border-green-500 bg-green-50 dark:bg-green-950 p-4 space-y-2">
          <p className="text-sm font-semibold text-green-800 dark:text-green-200">
            API-ключ создан. Скопируйте его — он больше не будет показан.
          </p>
          <code className="block font-mono text-sm break-all bg-white dark:bg-black/30 rounded p-2 select-all">
            {createdKey}
          </code>
          <div className="flex gap-3">
            <button
              onClick={() => navigator.clipboard.writeText(createdKey)}
              className="text-xs text-green-700 dark:text-green-300 underline"
            >
              Скопировать
            </button>
            <button
              onClick={() => setCreatedKey(null)}
              className="text-xs text-muted-foreground underline"
            >
              Закрыть
            </button>
          </div>
        </div>
      )}

      <div className="flex justify-between items-center">
        <p className="text-sm text-muted-foreground">
          Сервисные аккаунты для внешних интеграций
        </p>
        <button
          onClick={() => setShowForm(true)}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
        >
          Создать ключ
        </button>
      </div>

      {showForm && (
        <div className="rounded-lg border border-border bg-card p-4 space-y-3">
          <h3 className="text-sm font-semibold">Новый API-ключ</h3>
          <div>
            <label className="text-xs text-muted-foreground">Название</label>
            <input
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="Telegram-бот, Celery и т.д."
              className="mt-1 w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Разрешения</label>
            <div className="mt-1 flex flex-wrap gap-2">
              {ALL_SCOPES.map((s) => (
                <label
                  key={s}
                  className="flex items-center gap-1.5 text-xs cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={newScopes.includes(s)}
                    onChange={() => toggleScope(s)}
                  />
                  {s}
                </label>
              ))}
            </div>
          </div>
          <div className="flex gap-2">
            <button
              onClick={create}
              disabled={creating}
              className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
            >
              {creating ? "Создание..." : "Создать"}
            </button>
            <button
              onClick={() => setShowForm(false)}
              className="px-3 py-1.5 rounded border border-border text-sm"
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {loading && <p className="text-sm text-muted-foreground">Загрузка...</p>}
      {error && <p className="text-sm text-destructive">Ошибка: {error}</p>}

      {!loading && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted text-muted-foreground text-xs uppercase">
              <tr>
                <th className="px-4 py-2 text-left">Название</th>
                <th className="px-4 py-2 text-left">Разрешения</th>
                <th className="px-4 py-2 text-left">Использован</th>
                <th className="px-4 py-2 text-left">Истекает</th>
                <th className="px-4 py-2 text-left">Статус</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {keys.map((k) => (
                <tr key={k.id} className="hover:bg-muted/30">
                  <td className="px-4 py-2 font-medium">{k.name}</td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {k.scopes.join(", ") || "—"}
                  </td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {k.last_used_at
                      ? new Date(k.last_used_at).toLocaleString("ru")
                      : "—"}
                  </td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {k.expires_at
                      ? new Date(k.expires_at).toLocaleDateString("ru")
                      : "∞"}
                  </td>
                  <td className="px-4 py-2">
                    {k.is_active ? (
                      <span className="text-xs text-green-600">Активен</span>
                    ) : (
                      <span className="text-xs text-red-500">Отозван</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {k.is_active && (
                      <button
                        onClick={() => revoke(k.id)}
                        className="text-xs text-destructive hover:underline"
                      >
                        Отозвать
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {keys.length === 0 && (
                <tr>
                  <td
                    colSpan={6}
                    className="px-4 py-4 text-center text-sm text-muted-foreground"
                  >
                    Нет API-ключей
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function AdminApiKeysPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <ApiKeysContent />
    </ProtectedRoute>
  );
}
