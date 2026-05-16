"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

interface AuditLogOut {
  id: string;
  action: string;
  entity_type: string;
  entity_id: string | null;
  user_id: string | null;
  ip_address: string | null;
  details: Record<string, unknown> | null;
  timestamp: string;
}

const KNOWN_ACTIONS = [
  "admin.update_user",
  "admin.deactivate_user",
  "admin.create_api_key",
  "admin.revoke_api_key",
  "approval.request",
  "approval.approved",
  "approval.rejected",
  "approval.delegated",
  "injection_attempt",
];

function AuditContent() {
  const [items, setItems] = useState<AuditLogOut[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [userId, setUserId] = useState("");
  const [action, setAction] = useState("");
  const [entityType, setEntityType] = useState("");

  function load() {
    setLoading(true);
    const params = new URLSearchParams({ limit: "100" });
    if (userId) params.set("user_id", userId);
    if (action) params.set("action", action);
    if (entityType) params.set("entity_type", entityType);

    fetch(`${API}/api/admin/audit-logs?${params}`, { credentials: "include" })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d) => {
        setItems(d.items);
        setTotal(d.total);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, [userId, action, entityType]); // eslint-disable-line

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        <input
          type="text"
          placeholder="User sub или email..."
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          className="border border-border rounded px-3 py-1.5 text-sm bg-background w-52"
        />
        <select
          value={action}
          onChange={(e) => setAction(e.target.value)}
          className="border border-border rounded px-3 py-1.5 text-sm bg-background"
        >
          <option value="">Все действия</option>
          {KNOWN_ACTIONS.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Тип сущности..."
          value={entityType}
          onChange={(e) => setEntityType(e.target.value)}
          className="border border-border rounded px-3 py-1.5 text-sm bg-background w-36"
        />
      </div>

      {loading && <p className="text-sm text-muted-foreground">Загрузка...</p>}
      {error && <p className="text-sm text-destructive">Ошибка: {error}</p>}

      {!loading && (
        <>
          <p className="text-xs text-muted-foreground">Всего: {total}</p>
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-xs">
              <thead className="bg-muted text-muted-foreground uppercase">
                <tr>
                  <th className="px-3 py-2 text-left">Время</th>
                  <th className="px-3 py-2 text-left">Действие</th>
                  <th className="px-3 py-2 text-left">Сущность</th>
                  <th className="px-3 py-2 text-left">Пользователь</th>
                  <th className="px-3 py-2 text-left">IP</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {items.map((log) => (
                  <tr key={log.id} className="hover:bg-muted/30">
                    <td className="px-3 py-1.5 whitespace-nowrap text-muted-foreground">
                      {new Date(log.timestamp).toLocaleString("ru")}
                    </td>
                    <td className="px-3 py-1.5 font-mono">
                      {log.action === "injection_attempt" ? (
                        <span className="text-orange-600 font-semibold">
                          {log.action}
                        </span>
                      ) : (
                        log.action
                      )}
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground">
                      {log.entity_type}
                      {log.entity_id && (
                        <span className="font-mono ml-1 text-muted-foreground/60">
                          {log.entity_id.slice(0, 8)}…
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-muted-foreground">
                      {log.user_id ? log.user_id.slice(0, 16) + "…" : "—"}
                    </td>
                    <td className="px-3 py-1.5 text-muted-foreground">
                      {log.ip_address ?? "—"}
                    </td>
                  </tr>
                ))}
                {items.length === 0 && (
                  <tr>
                    <td
                      colSpan={5}
                      className="px-3 py-4 text-center text-muted-foreground"
                    >
                      Записей нет
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

export default function AdminAuditPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <AuditContent />
    </ProtectedRoute>
  );
}
