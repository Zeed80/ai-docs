"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch } from "@/lib/auth";

import { useEffect, useState, useCallback } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AgentWorkspaceBlocks } from "@/components/workspace/agent-workspace-blocks";

const API = getApiBaseUrl();

interface FeedItem {
  id: string;
  type: "approval" | "anomaly" | "quarantine";
  priority: "critical" | "warning" | "info";
  title: string;
  summary: string;
  entity_type: string;
  entity_id: string;
  created_at: string;
  meta: Record<string, string>;
}

const TYPE_CONFIG = {
  approval: {
    label: "Согласование",
    icon: (
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
          d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
        />
      </svg>
    ),
    color: "text-amber-400",
    bg: "bg-amber-950/30 border-amber-700/40",
    dot: "bg-amber-400",
  },
  anomaly: {
    label: "Аномалия",
    icon: (
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
          d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
        />
      </svg>
    ),
    color: "text-red-400",
    bg: "bg-red-950/30 border-red-700/40",
    dot: "bg-red-400",
  },
  quarantine: {
    label: "Карантин",
    icon: (
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
          d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
        />
      </svg>
    ),
    color: "text-orange-400",
    bg: "bg-orange-950/30 border-orange-700/40",
    dot: "bg-orange-400",
  },
};

function timeAgo(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1) return "только что";
  if (m < 60) return `${m} мин назад`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ч назад`;
  return `${Math.floor(h / 24)} дн назад`;
}

function ApprovalActions({
  item,
  onDone,
}: {
  item: FeedItem;
  onDone: () => void;
}) {
  const [loading, setLoading] = useState(false);
  async function decide(approved: boolean) {
    setLoading(true);
    await apiFetch(
      `${API}/api/approvals/${item.id}/${approved ? "approve" : "reject"}`,
      { method: "POST" },
    ).catch(() => {});
    setLoading(false);
    onDone();
  }
  return (
    <div className="flex gap-2 mt-3">
      <button
        onClick={() => decide(true)}
        disabled={loading}
        className="px-3 py-1.5 text-xs font-medium bg-green-700 hover:bg-green-600 text-white rounded transition-colors disabled:opacity-50"
      >
        Утвердить
      </button>
      <button
        onClick={() => decide(false)}
        disabled={loading}
        className="px-3 py-1.5 text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 rounded transition-colors disabled:opacity-50"
      >
        Отклонить
      </button>
    </div>
  );
}

function AnomalyActions({
  item,
  onDone,
}: {
  item: FeedItem;
  onDone: () => void;
}) {
  const [loading, setLoading] = useState(false);
  async function resolve(resolution: "resolved" | "false_positive") {
    setLoading(true);
    await apiFetch(`${API}/api/anomalies/${item.id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolution }),
    }).catch(() => {});
    setLoading(false);
    onDone();
  }
  return (
    <div className="flex gap-2 mt-3">
      <button
        onClick={() => resolve("resolved")}
        disabled={loading}
        className="px-3 py-1.5 text-xs font-medium bg-green-700 hover:bg-green-600 text-white rounded transition-colors disabled:opacity-50"
      >
        Решить
      </button>
      <button
        onClick={() => resolve("false_positive")}
        disabled={loading}
        className="px-3 py-1.5 text-xs font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 rounded transition-colors disabled:opacity-50"
      >
        Ложная
      </button>
      <Link
        href="/anomalies"
        className="px-3 py-1.5 text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-400 rounded transition-colors inline-block"
      >
        Все аномалии →
      </Link>
    </div>
  );
}

function QuarantineActions({
  item,
  onDone,
}: {
  item: FeedItem;
  onDone: () => void;
}) {
  const [loading, setLoading] = useState(false);
  async function decide(action: "release" | "delete") {
    setLoading(true);
    const url =
      action === "release"
        ? `${API}/api/quarantine/${item.id}/release`
        : `${API}/api/quarantine/${item.id}`;
    await fetch(url, {
      method: action === "release" ? "POST" : "DELETE",
    }).catch(() => {});
    setLoading(false);
    onDone();
  }
  return (
    <div className="flex gap-2 mt-3">
      <button
        onClick={() => decide("release")}
        disabled={loading}
        className="px-3 py-1.5 text-xs font-medium bg-green-700 hover:bg-green-600 text-white rounded transition-colors disabled:opacity-50"
      >
        Разрешить
      </button>
      <button
        onClick={() => decide("delete")}
        disabled={loading}
        className="px-3 py-1.5 text-xs font-medium bg-red-800 hover:bg-red-700 text-white rounded transition-colors disabled:opacity-50"
      >
        Удалить
      </button>
    </div>
  );
}

interface SystemMetrics {
  documents: { total: number; processing: number; needs_review: number };
  invoices: { needs_review: number };
  approvals: { pending: number; overdue: number };
  anomalies: { open: number };
  workspace: { block_count: number };
}

function SystemHealthBar() {
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);

  useEffect(() => {
    function load() {
      fetch(`${API}/api/metrics`)
        .then((r) => r.json())
        .then((data) => {
          if (
            data &&
            data.documents &&
            data.invoices &&
            data.approvals &&
            data.anomalies
          ) {
            setMetrics(data as SystemMetrics);
          }
        })
        .catch(() => {});
    }
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);

  if (!metrics) return null;

  const stats = [
    {
      label: "Документов",
      value: metrics.documents.total,
      sub:
        metrics.documents.needs_review > 0
          ? `${metrics.documents.needs_review} на проверке`
          : null,
      color:
        metrics.documents.needs_review > 0
          ? "text-amber-400"
          : "text-slate-400",
      href: "/documents",
    },
    {
      label: "Счетов к проверке",
      value: metrics.invoices.needs_review,
      sub: null,
      color:
        metrics.invoices.needs_review > 0 ? "text-amber-400" : "text-green-400",
      href: "/invoices",
    },
    {
      label: "Согласований",
      value: metrics.approvals.pending,
      sub:
        metrics.approvals.overdue > 0
          ? `${metrics.approvals.overdue} просрочено`
          : null,
      color:
        metrics.approvals.overdue > 0
          ? "text-red-400"
          : metrics.approvals.pending > 0
            ? "text-amber-400"
            : "text-green-400",
      href: "/approvals",
    },
    {
      label: "Аномалий",
      value: metrics.anomalies.open,
      sub: null,
      color: metrics.anomalies.open > 0 ? "text-red-400" : "text-green-400",
      href: "/anomalies",
    },
    {
      label: "Блоков на столе",
      value: metrics.workspace.block_count,
      sub: null,
      color: "text-slate-400",
      href: "/",
    },
  ];

  return (
    <div className="px-6 py-2 border-b border-slate-700/30 flex gap-4 overflow-x-auto">
      {stats.map((s) => (
        <a
          key={s.label}
          href={s.href}
          className="flex items-baseline gap-1.5 shrink-0 group"
        >
          <span
            className={`text-base font-bold ${s.color} group-hover:opacity-80`}
          >
            {s.value}
          </span>
          <span className="text-[10px] text-slate-500 group-hover:text-slate-400">
            {s.label}
          </span>
          {s.sub && <span className="text-[10px] text-red-400">· {s.sub}</span>}
        </a>
      ))}
    </div>
  );
}

function CreateCaseForm() {
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [customer, setCustomer] = useState("");
  const [taskDesc, setTaskDesc] = useState("");
  const [creating, setCreating] = useState(false);

  async function create() {
    if (!title.trim()) return;
    setCreating(true);
    try {
      const res = await apiFetch(`${API}/api/cases`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title.trim(),
          customer: customer || null,
          task_description: taskDesc || null,
        }),
      });
      if (res.ok) {
        const created = await res.json();
        router.push(`/cases/${created.id}`);
      }
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="mb-5 bg-slate-800/80 border border-slate-700/60 rounded-lg p-4 space-y-2 max-w-2xl">
      <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wide">
        Новый кейс
      </h3>
      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && create()}
        placeholder="Название кейса: Вал Ø25 / счет Hoffmann"
        className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
      />
      <input
        type="text"
        value={customer}
        onChange={(e) => setCustomer(e.target.value)}
        placeholder="Заказчик"
        className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
      />
      <input
        type="text"
        value={taskDesc}
        onChange={(e) => setTaskDesc(e.target.value)}
        placeholder="Что нужно сделать технологу?"
        className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
      />
      <button
        onClick={create}
        disabled={creating || !title.trim()}
        className="px-4 py-1.5 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
      >
        {creating ? "Создаю..." : "Создать кейс"}
      </button>
    </div>
  );
}

export default function FeedPage() {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    const res = await fetch(`${API}/api/dashboard/feed`).catch(() => null);
    if (!res) return;
    const data = await res.json();
    setItems(data.items ?? []);
    setTotal(data.total ?? 0);
    setLoading(false);
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-6 py-4 border-b border-slate-700/50 flex items-center justify-between">
        <div>
          <h1 className="text-base font-semibold text-slate-100">
            Требует решения
          </h1>
          <p className="text-xs text-slate-400 mt-0.5">
            Света обрабатывает документы и поднимает важное сюда
          </p>
        </div>
        {total > 0 && (
          <span className="text-xs font-bold bg-red-600 text-white rounded-full px-2.5 py-1">
            {total}
          </span>
        )}
      </div>

      {/* System health bar */}
      <SystemHealthBar />

      {/* Feed */}
      <div className="flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        <CreateCaseForm />
        <AgentWorkspaceBlocks className="mb-4 min-h-[calc(100vh-8.5rem)]" />
        {loading ? (
          <div className="space-y-3">
            {[...Array(3)].map((_, i) => (
              <div
                key={i}
                className="h-24 rounded-lg bg-slate-800/50 animate-pulse"
              />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full text-center pb-20">
            <div className="w-16 h-16 rounded-full bg-slate-800 flex items-center justify-center mb-4">
              <svg
                className="w-8 h-8 text-green-400"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={1.5}
                  d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
                />
              </svg>
            </div>
            <p className="font-medium text-slate-300">Всё в порядке</p>
            <p className="text-sm text-slate-400 mt-1">
              Нет элементов, требующих вашего решения
            </p>
            <p className="text-xs text-slate-400 mt-3">
              Спросите Свету о чём-нибудь →
            </p>
          </div>
        ) : (
          <div className="space-y-2 max-w-2xl">
            {items.map((item) => {
              const cfg = TYPE_CONFIG[item.type];
              return (
                <div
                  key={item.id}
                  className={`rounded-lg border p-4 ${cfg.bg} transition-colors hover:brightness-110`}
                >
                  <div className="flex items-start gap-3">
                    <div className={`mt-0.5 shrink-0 ${cfg.color}`}>
                      {cfg.icon}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span
                          className={`text-[10px] font-semibold uppercase tracking-wider ${cfg.color}`}
                        >
                          {cfg.label}
                        </span>
                        {item.priority === "critical" && (
                          <span className="text-[10px] font-bold bg-red-600 text-white rounded px-1.5 py-0.5 uppercase tracking-wide">
                            критично
                          </span>
                        )}
                        <span className="text-[10px] text-slate-500 ml-auto">
                          {timeAgo(item.created_at)}
                        </span>
                      </div>
                      <p className="text-sm font-medium text-slate-100 mt-1">
                        {item.title}
                      </p>
                      {item.summary && (
                        <p className="text-xs text-slate-400 mt-0.5">
                          {item.summary}
                        </p>
                      )}
                      {/* Entity link */}
                      <div className="mt-2">
                        <Link
                          href={`/${item.entity_type}s/${item.entity_id}`}
                          className="text-[11px] text-blue-400 hover:text-blue-300 underline-offset-2 hover:underline"
                        >
                          Открыть {item.entity_type} →
                        </Link>
                      </div>
                      {/* Actions */}
                      {item.type === "approval" && (
                        <ApprovalActions item={item} onDone={load} />
                      )}
                      {item.type === "quarantine" && (
                        <QuarantineActions item={item} onDone={load} />
                      )}
                      {item.type === "anomaly" && (
                        <AnomalyActions item={item} onDone={load} />
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
