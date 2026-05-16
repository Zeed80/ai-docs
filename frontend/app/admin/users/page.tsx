"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

const ROLE_LABELS: Record<string, string> = {
  admin: "Администратор",
  manager: "Менеджер",
  accountant: "Бухгалтер",
  buyer: "Закупщик",
  engineer: "Инженер",
  viewer: "Наблюдатель",
};

interface UserOut {
  sub: string;
  email: string;
  name: string;
  role: string;
  is_active: boolean;
  last_seen_at: string | null;
  created_at: string;
}

interface UserListResponse {
  items: UserOut[];
  total: number;
}

const EMPTY_FORM = {
  name: "",
  email: "",
  role: "viewer",
  preferred_username: "",
};

function CreateUserModal({
  onCreated,
  onClose,
}: {
  onCreated: () => void;
  onClose: () => void;
}) {
  const [form, setForm] = useState(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim() || !form.email.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/admin/users`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify(form),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      onCreated();
      onClose();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-background rounded-xl border border-border shadow-xl w-full max-w-md mx-4 p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold">Создать пользователя</h2>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground"
          >
            ✕
          </button>
        </div>

        <form onSubmit={submit} className="space-y-3">
          <div>
            <label className="text-xs text-muted-foreground block mb-1">
              Имя <span className="text-destructive">*</span>
            </label>
            <input
              type="text"
              required
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="Иван Петров"
              className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
            />
          </div>

          <div>
            <label className="text-xs text-muted-foreground block mb-1">
              Email <span className="text-destructive">*</span>
            </label>
            <input
              type="email"
              required
              value={form.email}
              onChange={(e) =>
                setForm((f) => ({ ...f, email: e.target.value }))
              }
              placeholder="ivan@company.ru"
              className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
            />
          </div>

          <div>
            <label className="text-xs text-muted-foreground block mb-1">
              Логин (необязательно)
            </label>
            <input
              type="text"
              value={form.preferred_username}
              onChange={(e) =>
                setForm((f) => ({ ...f, preferred_username: e.target.value }))
              }
              placeholder="ivan.petrov"
              className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
            />
          </div>

          <div>
            <label className="text-xs text-muted-foreground block mb-1">
              Роль
            </label>
            <select
              value={form.role}
              onChange={(e) => setForm((f) => ({ ...f, role: e.target.value }))}
              className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
            >
              {Object.entries(ROLE_LABELS).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </div>

          {error && <p className="text-xs text-destructive">Ошибка: {error}</p>}

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted"
            >
              Отмена
            </button>
            <button
              type="submit"
              disabled={saving || !form.name.trim() || !form.email.trim()}
              className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
            >
              {saving ? "Создание..." : "Создать"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function UsersContent() {
  const [data, setData] = useState<UserListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [roleFilter, setRoleFilter] = useState("");
  const [activeFilter, setActiveFilter] = useState<string>("");
  const [q, setQ] = useState("");
  const [showCreate, setShowCreate] = useState(false);

  function load() {
    setLoading(true);
    const params = new URLSearchParams();
    if (roleFilter) params.set("role", roleFilter);
    if (activeFilter !== "") params.set("is_active", activeFilter);
    if (q) params.set("q", q);
    params.set("limit", "100");

    fetch(`${API}/api/admin/users?${params}`, { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, [roleFilter, activeFilter, q]); // eslint-disable-line

  async function deactivate(sub: string) {
    if (!confirm("Деактивировать пользователя?")) return;
    await fetch(
      `${API}/api/admin/users/${encodeURIComponent(sub)}/deactivate`,
      {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
      },
    );
    load();
  }

  async function activate(sub: string) {
    await fetch(`${API}/api/admin/users/${encodeURIComponent(sub)}`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ is_active: true }),
    });
    load();
  }

  return (
    <div className="space-y-4">
      {showCreate && (
        <CreateUserModal
          onCreated={load}
          onClose={() => setShowCreate(false)}
        />
      )}

      <div className="flex flex-wrap gap-2 items-center">
        <input
          type="search"
          placeholder="Поиск по имени или email..."
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="border border-border rounded px-3 py-1.5 text-sm bg-background w-64"
        />
        <select
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value)}
          className="border border-border rounded px-3 py-1.5 text-sm bg-background"
        >
          <option value="">Все роли</option>
          {Object.entries(ROLE_LABELS).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
        <select
          value={activeFilter}
          onChange={(e) => setActiveFilter(e.target.value)}
          className="border border-border rounded px-3 py-1.5 text-sm bg-background"
        >
          <option value="">Все</option>
          <option value="true">Активные</option>
          <option value="false">Деактивированные</option>
        </select>

        <button
          onClick={() => setShowCreate(true)}
          className="ml-auto px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm flex items-center gap-1.5"
        >
          <svg
            className="w-4 h-4"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M12 4v16m8-8H4"
            />
          </svg>
          Создать пользователя
        </button>
      </div>

      {loading && <p className="text-sm text-muted-foreground">Загрузка...</p>}
      {error && <p className="text-sm text-destructive">Ошибка: {error}</p>}

      {data && (
        <>
          <p className="text-xs text-muted-foreground">Всего: {data.total}</p>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-muted text-muted-foreground text-xs uppercase">
                <tr>
                  <th className="px-4 py-2 text-left">Имя</th>
                  <th className="px-4 py-2 text-left">Email</th>
                  <th className="px-4 py-2 text-left">Роль</th>
                  <th className="px-4 py-2 text-left">Статус</th>
                  <th className="px-4 py-2 text-left">Последний вход</th>
                  <th className="px-4 py-2" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.items.map((u) => (
                  <tr
                    key={u.sub}
                    className="hover:bg-muted/40 transition-colors"
                  >
                    <td className="px-4 py-2 font-medium">
                      <Link
                        href={`/admin/users/${encodeURIComponent(u.sub)}`}
                        className="hover:underline"
                      >
                        {u.name}
                      </Link>
                      {u.sub.startsWith("local:") && (
                        <span className="ml-1.5 text-xs text-amber-600 bg-amber-50 px-1.5 py-0.5 rounded">
                          не авторизовался
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-muted-foreground">
                      {u.email}
                    </td>
                    <td className="px-4 py-2">
                      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs bg-muted">
                        {ROLE_LABELS[u.role] ?? u.role}
                      </span>
                    </td>
                    <td className="px-4 py-2">
                      {u.is_active ? (
                        <span className="text-green-600 text-xs">Активен</span>
                      ) : (
                        <span className="text-red-500 text-xs">
                          Деактивирован
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-muted-foreground text-xs">
                      {u.last_seen_at
                        ? new Date(u.last_seen_at).toLocaleString("ru")
                        : "—"}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <div className="flex justify-end gap-2">
                        <Link
                          href={`/admin/users/${encodeURIComponent(u.sub)}`}
                          className="text-xs text-primary hover:underline"
                        >
                          Изменить
                        </Link>
                        {u.is_active ? (
                          <button
                            onClick={() => deactivate(u.sub)}
                            className="text-xs text-destructive hover:underline"
                          >
                            Деактивировать
                          </button>
                        ) : (
                          <button
                            onClick={() => activate(u.sub)}
                            className="text-xs text-green-600 hover:underline"
                          >
                            Активировать
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
                {data.items.length === 0 && (
                  <tr>
                    <td
                      colSpan={6}
                      className="px-4 py-8 text-center text-sm text-muted-foreground"
                    >
                      Пользователи не найдены
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

export default function AdminUsersPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <UsersContent />
    </ProtectedRoute>
  );
}
