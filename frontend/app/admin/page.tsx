"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useEffect, useState } from "react";

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
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${
        ok ? "bg-green-900/40 text-green-400" : "bg-red-900/40 text-red-400"
      }`}
    >
      <span
        className={`w-1.5 h-1.5 rounded-full ${ok ? "bg-green-400" : "bg-red-400"}`}
      />
      {ok ? "OK" : value}
    </span>
  );
}

export default function AdminPage() {
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
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;
  if (error) return <p className="text-sm text-red-400">Ошибка: {error}</p>;
  if (!status) return null;

  const infra = [
    { label: "База данных", value: status.db },
    { label: "Redis", value: status.redis },
    { label: "Celery", value: status.celery },
  ];

  return (
    <div className="space-y-6">
      {/* KPI cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        <div className="bg-card border border-border rounded-lg p-4">
          <p className="text-xs text-muted-foreground mb-1">
            Активные пользователи
          </p>
          <p className="text-2xl font-bold">{status.active_users_count}</p>
        </div>
        <div className="bg-card border border-border rounded-lg p-4">
          <p className="text-xs text-muted-foreground mb-1">
            Ожидают согласования
          </p>
          <p className="text-2xl font-bold text-amber-400">
            {status.pending_approvals_count}
          </p>
        </div>
      </div>

      {/* Infrastructure */}
      <div className="bg-card border border-border rounded-lg p-4">
        <h2 className="text-sm font-semibold mb-3">Инфраструктура</h2>
        <div className="space-y-2">
          {infra.map(({ label, value }) => (
            <div
              key={label}
              className="flex items-center justify-between text-sm"
            >
              <span className="text-muted-foreground">{label}</span>
              <StatusBadge value={value} />
            </div>
          ))}
        </div>
      </div>

      {/* AI Providers */}
      <div className="bg-card border border-border rounded-lg p-4">
        <h2 className="text-sm font-semibold mb-3">AI-провайдеры</h2>
        {Object.keys(status.ai_providers).length === 0 ? (
          <p className="text-xs text-muted-foreground">Нет данных</p>
        ) : (
          <div className="space-y-2">
            {Object.entries(status.ai_providers).map(([provider, value]) => (
              <div
                key={provider}
                className="flex items-center justify-between text-sm"
              >
                <span className="text-muted-foreground">{provider}</span>
                <StatusBadge value={value} />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Quick links */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { href: "/admin/users", label: "Пользователи" },
          { href: "/admin/permissions", label: "Права доступа" },
          { href: "/admin/audit", label: "Журнал аудита" },
          { href: "/admin/api-keys", label: "API-ключи" },
        ].map(({ href, label }) => (
          <a
            key={href}
            href={href}
            className="block bg-card border border-border rounded-lg p-3 text-sm font-medium hover:bg-muted transition-colors text-center"
          >
            {label}
          </a>
        ))}
      </div>
    </div>
  );
}
