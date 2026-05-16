"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

interface SystemStatus {
  db: string;
  redis: string;
  celery: string;
  ai_providers: Record<string, string>;
  active_users_count: number;
  pending_approvals_count: number;
}

function StatusBadge({ value }: { value: string }) {
  const ok = value === "ok";
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${ok ? "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200" : "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200"}`}
    >
      {ok ? "ОК" : value}
    </span>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-bold">{value}</p>
    </div>
  );
}

function SystemContent() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/api/admin/system-status`, { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setStatus)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading)
    return <div className="text-muted-foreground text-sm">Загрузка...</div>;
  if (error)
    return <div className="text-destructive text-sm">Ошибка: {error}</div>;
  if (!status) return null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        <StatCard
          label="Активных пользователей"
          value={status.active_users_count}
        />
        <StatCard
          label="Ожидают решения"
          value={status.pending_approvals_count}
        />
      </div>

      <div className="rounded-lg border border-border bg-card p-4 space-y-3">
        <h2 className="text-sm font-semibold">Состояние системы</h2>
        <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">База данных</span>
            <StatusBadge value={status.db} />
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">Redis</span>
            <StatusBadge value={status.redis} />
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-muted-foreground">Celery</span>
            <StatusBadge value={status.celery} />
          </div>
        </div>
        {Object.keys(status.ai_providers).length > 0 && (
          <div>
            <p className="text-xs text-muted-foreground mb-2">AI-провайдеры</p>
            <div className="flex flex-wrap gap-2">
              {Object.entries(status.ai_providers).map(([name, val]) => (
                <div key={name} className="flex items-center gap-1.5">
                  <span className="text-xs">{name}</span>
                  <StatusBadge value={val} />
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function AdminSystemPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <SystemContent />
    </ProtectedRoute>
  );
}
