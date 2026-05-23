"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
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
  technologist: "Технолог",
  viewer: "Наблюдатель",
};

interface UserOut {
  sub: string;
  email: string;
  name: string;
  preferred_username: string;
  role: string;
  is_active: boolean;
  last_seen_at: string | null;
  created_at: string;
}

function SetPasswordSection({ userSub }: { userSub: string }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Пароли не совпадают");
      return;
    }
    if (password.length < 8) {
      setError("Минимум 8 символов");
      return;
    }
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(userSub)}/set-password`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({ password }),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setPassword("");
      setConfirm("");
      setSuccess(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <h3 className="text-sm font-medium">Установить пароль</h3>
      <form onSubmit={submit} className="space-y-2">
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Новый пароль (мин. 8 символов)"
          minLength={8}
          required
          className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
        />
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="Повторите пароль"
          required
          className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
        />
        {error && <p className="text-xs text-destructive">{error}</p>}
        {success && (
          <p className="text-xs text-green-600">Пароль успешно изменён</p>
        )}
        <button
          type="submit"
          disabled={saving || !password || !confirm}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
        >
          {saving ? "Сохранение..." : "Установить пароль"}
        </button>
      </form>
    </div>
  );
}

function UserEditContent() {
  const { sub } = useParams<{ sub: string }>();
  const router = useRouter();
  const [user, setUser] = useState<UserOut | null>(null);
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/admin/users/${encodeURIComponent(sub)}`, {
      credentials: "include",
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((u: UserOut) => {
        setUser(u);
        setRole(u.role);
        setName(u.name);
      })
      .catch((e) => setError(e.message));
  }, [sub]);

  async function save() {
    setSaving(true);
    setSuccess(false);
    setError(null);
    try {
      const changes: Record<string, unknown> = {};
      if (role !== user!.role) changes.role = role;
      if (name.trim() && name.trim() !== user!.name) changes.name = name.trim();
      if (Object.keys(changes).length === 0) return;

      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(sub)}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify(changes),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setSuccess(true);
      router.refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function toggleActive() {
    if (!user) return;
    const res = await fetch(
      `${API}/api/admin/users/${encodeURIComponent(sub)}`,
      {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ is_active: !user.is_active }),
      },
    );
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setError(d.detail ?? `HTTP ${res.status}`);
      return;
    }
    const updated: UserOut = await res.json();
    setUser(updated);
  }

  async function deleteUser() {
    setDeleting(true);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(sub)}/deactivate`,
        {
          method: "POST",
          credentials: "include",
          headers: csrfHeaders(),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      router.push("/admin/users");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
      setDeleteConfirm(false);
    }
  }

  if (error && !user)
    return <p className="text-sm text-destructive">Ошибка: {error}</p>;
  if (!user)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;

  const isDirty =
    role !== user.role || (name.trim() && name.trim() !== user.name);

  return (
    <div className="max-w-md space-y-4">
      <button
        onClick={() => router.push("/admin/users")}
        className="text-xs text-muted-foreground hover:underline block"
      >
        ← Назад к пользователям
      </button>

      <div>
        <h2 className="text-base font-semibold">{user.name}</h2>
        <p className="text-sm text-muted-foreground">{user.email}</p>
        <p className="text-xs text-muted-foreground mt-0.5">
          sub: <code className="font-mono">{user.sub}</code>
        </p>
      </div>

      {/* Profile */}
      <div className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="text-sm font-medium">Профиль</h3>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Имя
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          />
        </div>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Роль
          </label>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          >
            {Object.entries(ROLE_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </div>

        <div className="text-xs text-muted-foreground space-y-1">
          <p>
            Статус:{" "}
            {user.is_active ? (
              <span className="text-green-600">Активен</span>
            ) : (
              <span className="text-red-500">Деактивирован</span>
            )}
          </p>
          <p>
            Последний вход:{" "}
            {user.last_seen_at
              ? new Date(user.last_seen_at).toLocaleString("ru")
              : "—"}
          </p>
          <p>
            Зарегистрирован:{" "}
            {new Date(user.created_at).toLocaleDateString("ru")}
          </p>
        </div>

        {error && <p className="text-xs text-destructive">{error}</p>}
        {success && (
          <p className="text-xs text-green-600">Изменения сохранены</p>
        )}

        <button
          onClick={save}
          disabled={saving || !isDirty}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
        >
          {saving ? "Сохранение..." : "Сохранить"}
        </button>
      </div>

      {/* Password */}
      <SetPasswordSection userSub={sub} />

      {/* Activate / deactivate */}
      <div className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="text-sm font-medium">Доступ</h3>
        <button
          onClick={toggleActive}
          className={`px-3 py-1.5 rounded text-sm ${
            user.is_active
              ? "bg-amber-100 text-amber-700 hover:bg-amber-200"
              : "bg-green-100 text-green-700 hover:bg-green-200"
          }`}
        >
          {user.is_active ? "Деактивировать" : "Активировать"}
        </button>
        <p className="text-[10px] text-muted-foreground">
          Деактивированный пользователь не может войти, но данные сохраняются
        </p>
      </div>

      {/* Danger zone */}
      <div className="rounded-lg border border-destructive/40 p-4 space-y-3">
        <h3 className="text-sm font-medium text-destructive">Опасная зона</h3>
        {!deleteConfirm ? (
          <button
            onClick={() => setDeleteConfirm(true)}
            className="px-3 py-1.5 rounded border border-destructive text-destructive text-sm hover:bg-destructive/10"
          >
            Удалить пользователя
          </button>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-destructive">
              Пользователь будет деактивирован. Подтвердите:
            </p>
            <div className="flex gap-2">
              <button
                onClick={deleteUser}
                disabled={deleting}
                className="px-3 py-1.5 rounded bg-destructive text-destructive-foreground text-sm disabled:opacity-50"
              >
                {deleting ? "Удаление..." : "Да, удалить"}
              </button>
              <button
                onClick={() => setDeleteConfirm(false)}
                className="px-3 py-1.5 rounded border border-border text-sm"
              >
                Отмена
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function AdminUserEditPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <UserEditContent />
    </ProtectedRoute>
  );
}
