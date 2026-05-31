"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

interface DepartmentOut {
  id: string;
  name: string;
  code: string;
  parent_id: string | null;
  created_at: string;
}

interface DepartmentListResponse {
  items: DepartmentOut[];
  total: number;
}

function DepartmentsContent() {
  const [data, setData] = useState<DepartmentListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", code: "", parent_id: "" });
  const [saving, setSaving] = useState(false);

  function load() {
    setLoading(true);
    fetch(`${API}/api/admin/departments`, { credentials: "include" })
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
  }, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!form.name.trim() || !form.code.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`${API}/api/admin/departments`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({
          name: form.name.trim(),
          code: form.code.trim(),
          parent_id: form.parent_id || null,
        }),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setForm({ name: "", code: "", parent_id: "" });
      load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: string) {
    if (!confirm("Удалить отдел?")) return;
    const res = await fetch(`${API}/api/admin/departments/${id}`, {
      method: "DELETE",
      credentials: "include",
      headers: csrfHeaders(),
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setError(d.detail ?? `HTTP ${res.status}`);
      return;
    }
    load();
  }

  const nameById = (id: string | null) =>
    id ? (data?.items.find((d) => d.id === id)?.name ?? "—") : "—";

  return (
    <div className="space-y-4">
      <form onSubmit={create} className="flex flex-wrap items-end gap-2">
        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Название
          </label>
          <input
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="Закупки"
            className="border border-border rounded px-3 py-1.5 text-sm bg-background w-56"
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Код
          </label>
          <input
            value={form.code}
            onChange={(e) => setForm((f) => ({ ...f, code: e.target.value }))}
            placeholder="proc"
            className="border border-border rounded px-3 py-1.5 text-sm bg-background w-36"
          />
        </div>
        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Родительский отдел
          </label>
          <select
            value={form.parent_id}
            onChange={(e) =>
              setForm((f) => ({ ...f, parent_id: e.target.value }))
            }
            className="border border-border rounded px-3 py-1.5 text-sm bg-background w-56"
          >
            <option value="">— нет —</option>
            {data?.items.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </div>
        <button
          type="submit"
          disabled={saving || !form.name.trim() || !form.code.trim()}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
        >
          {saving ? "Создание..." : "Добавить отдел"}
        </button>
      </form>

      {error && <p className="text-sm text-destructive">Ошибка: {error}</p>}
      {loading && <p className="text-sm text-muted-foreground">Загрузка...</p>}

      {data && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-muted text-muted-foreground text-xs uppercase">
              <tr>
                <th className="px-4 py-2 text-left">Название</th>
                <th className="px-4 py-2 text-left">Код</th>
                <th className="px-4 py-2 text-left">Родитель</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {data.items.map((d) => (
                <tr key={d.id} className="hover:bg-muted/40 transition-colors">
                  <td className="px-4 py-2 font-medium">{d.name}</td>
                  <td className="px-4 py-2 text-muted-foreground">{d.code}</td>
                  <td className="px-4 py-2 text-muted-foreground">
                    {nameById(d.parent_id)}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <button
                      onClick={() => remove(d.id)}
                      className="text-xs text-destructive hover:underline"
                    >
                      Удалить
                    </button>
                  </td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr>
                  <td
                    colSpan={4}
                    className="px-4 py-8 text-center text-sm text-muted-foreground"
                  >
                    Отделы не созданы
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

export default function AdminDepartmentsPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <DepartmentsContent />
    </ProtectedRoute>
  );
}
