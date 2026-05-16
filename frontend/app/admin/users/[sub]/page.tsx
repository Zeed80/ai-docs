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

function UserEditContent() {
  const { sub } = useParams<{ sub: string }>();
  const router = useRouter();
  const [user, setUser] = useState<UserOut | null>(null);
  const [role, setRole] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

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
      })
      .catch((e) => setError(e.message));
  }, [sub]);

  async function save() {
    setSaving(true);
    setSuccess(false);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(sub)}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({ role }),
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

  if (error) return <p className="text-sm text-destructive">Ошибка: {error}</p>;
  if (!user)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;

  return (
    <div className="max-w-md space-y-4">
      <div>
        <button
          onClick={() => router.push("/admin/users")}
          className="text-xs text-muted-foreground hover:underline mb-4 block"
        >
          ← Назад к пользователям
        </button>
        <h2 className="text-base font-semibold">{user.name}</h2>
        <p className="text-sm text-muted-foreground">{user.email}</p>
      </div>

      <div className="rounded-lg border border-border p-4 space-y-3">
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
        {success && (
          <p className="text-xs text-green-600">Изменения сохранены</p>
        )}
        <div className="flex gap-2">
          <button
            onClick={save}
            disabled={saving || role === user.role}
            className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
          >
            {saving ? "Сохранение..." : "Сохранить"}
          </button>
        </div>
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
