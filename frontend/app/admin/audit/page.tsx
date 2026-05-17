"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { ProtectedRoute } from "@/components/auth/protected-route";
import Link from "next/link";

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

// Human-readable labels for known actions
const ACTION_LABELS: Record<
  string,
  { label: string; color: string; icon: string }
> = {
  "admin.update_user": {
    label: "Изменён пользователь",
    color: "text-blue-400",
    icon: "👤",
  },
  "admin.deactivate_user": {
    label: "Деактивирован пользователь",
    color: "text-red-400",
    icon: "🚫",
  },
  "admin.create_api_key": {
    label: "Создан API-ключ",
    color: "text-blue-400",
    icon: "🔑",
  },
  "admin.revoke_api_key": {
    label: "Отозван API-ключ",
    color: "text-orange-400",
    icon: "🔑",
  },
  "approval.request": {
    label: "Запрос согласования",
    color: "text-amber-400",
    icon: "📋",
  },
  "approval.approved": {
    label: "Согласовано",
    color: "text-green-400",
    icon: "✅",
  },
  "approval.rejected": {
    label: "Отклонено",
    color: "text-red-400",
    icon: "❌",
  },
  "approval.delegated": {
    label: "Делегировано",
    color: "text-purple-400",
    icon: "↗",
  },
  "approval.bulk_decided": {
    label: "Пакетное решение",
    color: "text-amber-400",
    icon: "📦",
  },
  injection_attempt: {
    label: "Попытка инъекции",
    color: "text-orange-500 font-bold",
    icon: "⚠",
  },
  "document.ingest": {
    label: "Документ загружен",
    color: "text-blue-400",
    icon: "📄",
  },
  "document.classify": {
    label: "Классификация",
    color: "text-blue-400",
    icon: "🏷",
  },
  "document.approve": {
    label: "Документ утверждён",
    color: "text-green-400",
    icon: "✅",
  },
  "invoice.approve": {
    label: "Счёт утверждён",
    color: "text-green-400",
    icon: "✅",
  },
  "invoice.reject": {
    label: "Счёт отклонён",
    color: "text-red-400",
    icon: "❌",
  },
  "compare.decide": {
    label: "Выбор поставщика",
    color: "text-blue-400",
    icon: "⚖",
  },
  "anomaly.resolve": {
    label: "Аномалия закрыта",
    color: "text-green-400",
    icon: "🔍",
  },
  "table.export_excel": {
    label: "Экспорт Excel",
    color: "text-slate-400",
    icon: "📊",
  },
  "table.export_1c": {
    label: "Экспорт 1С",
    color: "text-slate-400",
    icon: "📊",
  },
};

// Entity type → link builder
function entityLink(type: string, id: string | null): string | null {
  if (!id) return null;
  const map: Record<string, string> = {
    document: `/documents/${id}`,
    invoice: `/invoices/${id}`,
    approval: `/approvals`,
    anomaly: `/anomalies`,
    compare_session: `/compare/${id}`,
    collection: `/collections/${id}`,
    supplier: `/suppliers/${id}`,
  };
  return map[type] ?? null;
}

// Entity type → short label
const ENTITY_LABELS: Record<string, string> = {
  document: "Документ",
  invoice: "Счёт",
  approval: "Согласование",
  anomaly: "Аномалия",
  compare_session: "Сравнение",
  collection: "Коллекция",
  supplier: "Поставщик",
  user: "Пользователь",
  api_key: "API-ключ",
};

// Avatar initials from user_id
function userInitial(userId: string | null): string {
  if (!userId) return "?";
  return userId.replace(/-/g, "").slice(0, 2).toUpperCase();
}

const KNOWN_ACTIONS = Object.keys(ACTION_LABELS);

function AuditContent() {
  const [items, setItems] = useState<AuditLogOut[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [userId, setUserId] = useState("");
  const [action, setAction] = useState("");
  const [entityType, setEntityType] = useState("");
  const [view, setView] = useState<"timeline" | "table">("timeline");

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
        setItems(d.items ?? []);
        setTotal(d.total ?? 0);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
  }, [userId, action, entityType]); // eslint-disable-line

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <input
          type="text"
          placeholder="User ID или sub..."
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          className="border border-slate-600 rounded px-3 py-1.5 text-sm bg-slate-800 text-slate-200 placeholder-slate-500 w-52 focus:outline-none focus:border-blue-400"
        />
        <select
          value={action}
          onChange={(e) => setAction(e.target.value)}
          className="border border-slate-600 rounded px-3 py-1.5 text-sm bg-slate-800 text-slate-200 focus:outline-none focus:border-blue-400"
        >
          <option value="">Все действия</option>
          {KNOWN_ACTIONS.map((a) => (
            <option key={a} value={a}>
              {ACTION_LABELS[a]?.label ?? a}
            </option>
          ))}
        </select>
        <select
          value={entityType}
          onChange={(e) => setEntityType(e.target.value)}
          className="border border-slate-600 rounded px-3 py-1.5 text-sm bg-slate-800 text-slate-200 focus:outline-none focus:border-blue-400"
        >
          <option value="">Все типы</option>
          {Object.keys(ENTITY_LABELS).map((t) => (
            <option key={t} value={t}>
              {ENTITY_LABELS[t]}
            </option>
          ))}
        </select>
        <div className="ml-auto flex gap-1">
          {(["timeline", "table"] as const).map((v) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={`px-3 py-1 text-xs rounded ${view === v ? "bg-slate-600 text-slate-100" : "bg-slate-800 text-slate-400 hover:text-slate-200"}`}
            >
              {v === "timeline" ? "Лента" : "Таблица"}
            </button>
          ))}
        </div>
      </div>

      {loading && <p className="text-sm text-slate-500">Загрузка...</p>}
      {error && <p className="text-sm text-red-400">Ошибка: {error}</p>}

      {!loading && (
        <>
          <p className="text-xs text-slate-500">Найдено: {total}</p>

          {view === "timeline" ? (
            /* Timeline view */
            <div className="space-y-0">
              {items.length === 0 && (
                <p className="text-sm text-slate-500 py-8 text-center">
                  Записей нет
                </p>
              )}
              {items.map((log, idx) => {
                const meta = ACTION_LABELS[log.action];
                const link = entityLink(log.entity_type, log.entity_id);
                const isLast = idx === items.length - 1;

                return (
                  <div key={log.id} className="flex gap-3">
                    {/* Timeline spine */}
                    <div className="flex flex-col items-center">
                      <div
                        className={`w-7 h-7 rounded-full flex items-center justify-center text-xs shrink-0 ${
                          log.action === "injection_attempt"
                            ? "bg-orange-900/50 border border-orange-500"
                            : log.action.includes("approved") ||
                                log.action.includes("approve")
                              ? "bg-green-900/50 border border-green-600"
                              : log.action.includes("reject")
                                ? "bg-red-900/50 border border-red-600"
                                : "bg-slate-700 border border-slate-600"
                        }`}
                      >
                        {meta?.icon ?? "·"}
                      </div>
                      {!isLast && (
                        <div
                          className="w-px flex-1 bg-slate-700 my-0.5"
                          style={{ minHeight: 16 }}
                        />
                      )}
                    </div>

                    {/* Content */}
                    <div className="flex-1 pb-4 min-w-0">
                      <div className="flex items-start gap-2 flex-wrap">
                        <span
                          className={`text-sm font-medium ${meta?.color ?? "text-slate-300"}`}
                        >
                          {meta?.label ?? log.action}
                        </span>
                        {!meta && (
                          <span className="text-xs font-mono text-slate-500">
                            {log.action}
                          </span>
                        )}
                        <span className="text-xs text-slate-500 ml-auto shrink-0">
                          {new Date(log.timestamp).toLocaleString("ru-RU")}
                        </span>
                      </div>

                      {/* Entity */}
                      {log.entity_type && (
                        <div className="mt-0.5 flex items-center gap-1.5 text-xs text-slate-400">
                          <span>
                            {ENTITY_LABELS[log.entity_type] ?? log.entity_type}
                          </span>
                          {log.entity_id && link ? (
                            <Link
                              href={link}
                              className="font-mono text-blue-400 hover:text-blue-300 underline underline-offset-2"
                            >
                              {log.entity_id.slice(0, 8)}…
                            </Link>
                          ) : log.entity_id ? (
                            <span className="font-mono text-slate-500">
                              {log.entity_id.slice(0, 8)}…
                            </span>
                          ) : null}
                        </div>
                      )}

                      {/* User avatar + ID */}
                      {log.user_id && (
                        <div className="mt-1 flex items-center gap-1.5">
                          <span className="w-4 h-4 rounded-full bg-blue-700 flex items-center justify-center text-[8px] font-bold text-white">
                            {userInitial(log.user_id)}
                          </span>
                          <span className="text-[10px] font-mono text-slate-600">
                            {log.user_id.slice(0, 20)}…
                          </span>
                          {log.ip_address && (
                            <span className="text-[10px] text-slate-600">
                              · {log.ip_address}
                            </span>
                          )}
                        </div>
                      )}

                      {/* Key details */}
                      {log.details && Object.keys(log.details).length > 0 && (
                        <div className="mt-1 text-[10px] text-slate-600 font-mono truncate max-w-lg">
                          {Object.entries(log.details)
                            .filter(
                              ([, v]) =>
                                v !== null && v !== undefined && v !== "",
                            )
                            .slice(0, 3)
                            .map(([k, v]) => `${k}: ${String(v).slice(0, 40)}`)
                            .join(" · ")}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          ) : (
            /* Table view */
            <div className="overflow-x-auto rounded-lg border border-slate-700">
              <table className="w-full text-xs">
                <thead className="bg-slate-800 text-slate-400 uppercase">
                  <tr>
                    <th className="px-3 py-2 text-left">Время</th>
                    <th className="px-3 py-2 text-left">Действие</th>
                    <th className="px-3 py-2 text-left">Сущность</th>
                    <th className="px-3 py-2 text-left">Пользователь</th>
                    <th className="px-3 py-2 text-left">IP</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-700">
                  {items.map((log) => {
                    const meta = ACTION_LABELS[log.action];
                    const link = entityLink(log.entity_type, log.entity_id);
                    return (
                      <tr key={log.id} className="hover:bg-slate-700/30">
                        <td className="px-3 py-1.5 whitespace-nowrap text-slate-500">
                          {new Date(log.timestamp).toLocaleString("ru-RU")}
                        </td>
                        <td
                          className={`px-3 py-1.5 ${meta?.color ?? "text-slate-300"}`}
                        >
                          {meta?.icon} {meta?.label ?? log.action}
                        </td>
                        <td className="px-3 py-1.5 text-slate-400">
                          {ENTITY_LABELS[log.entity_type] ?? log.entity_type}
                          {log.entity_id && link ? (
                            <Link
                              href={link}
                              className="ml-1 font-mono text-blue-400 hover:underline"
                            >
                              {log.entity_id.slice(0, 8)}…
                            </Link>
                          ) : log.entity_id ? (
                            <span className="ml-1 font-mono text-slate-600">
                              {log.entity_id.slice(0, 8)}…
                            </span>
                          ) : null}
                        </td>
                        <td className="px-3 py-1.5 font-mono text-slate-500">
                          {log.user_id ? log.user_id.slice(0, 16) + "…" : "—"}
                        </td>
                        <td className="px-3 py-1.5 text-slate-500">
                          {log.ip_address ?? "—"}
                        </td>
                      </tr>
                    );
                  })}
                  {items.length === 0 && (
                    <tr>
                      <td
                        colSpan={5}
                        className="px-3 py-4 text-center text-slate-500"
                      >
                        Записей нет
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
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
