"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useCallback, useEffect, useState } from "react";

const API = getApiBaseUrl();

interface ApprovalItem {
  id: string;
  action_type: string;
  entity_type: string;
  entity_id: string;
  status: string;
  requested_by: string | null;
  assigned_to: string | null;
  context: Record<string, unknown> | null;
  decision_comment: string | null;
  created_at: string;
  expires_at: string | null;
  chain_root_id: string | null;
  chain_order: number | null;
}

function SlaBar({
  createdAt,
  expiresAt,
}: {
  createdAt: string;
  expiresAt: string;
}) {
  const start = new Date(createdAt).getTime();
  const end = new Date(expiresAt).getTime();
  const now = Date.now();
  const total = end - start;
  const elapsed = Math.max(0, Math.min(now - start, total));
  const pct = total > 0 ? Math.round((elapsed / total) * 100) : 0;
  const color =
    pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-green-500";
  return (
    <div className="mt-3">
      <div className="flex justify-between text-[10px] text-slate-500 mb-1">
        <span>SLA</span>
        <span>{pct}% использовано</span>
      </div>
      <div className="h-1.5 bg-slate-700 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

function expiryInfo(
  expiresAt: string | null,
): { label: string; cls: string } | null {
  if (!expiresAt) return null;
  const diff = new Date(expiresAt).getTime() - Date.now();
  if (diff < 0) return { label: "Просрочено", cls: "text-red-400" };
  const h = Math.floor(diff / 3_600_000);
  const m = Math.floor((diff % 3_600_000) / 60_000);
  const cls = diff < 7_200_000 ? "text-amber-400" : "text-slate-400";
  return { label: h > 0 ? `${h}ч ${m}м` : `${m}м`, cls };
}

const ACTION_LABELS: Record<string, string> = {
  "invoice.approve": "Утверждение счёта",
  "invoice.reject": "Отклонение счёта",
  "email.send": "Отправка письма",
  "anomaly.resolve": "Решение по аномалии",
  "table.apply_diff": "Применение изменений таблицы",
  "norm.activate_rule": "Активация правила нормализации",
  "compare.decide": "Выбор поставщика (КП)",
};

const ACTION_COLORS: Record<string, string> = {
  "invoice.approve": "bg-green-900/40 text-green-400",
  "invoice.reject": "bg-red-900/40 text-red-400",
  "email.send": "bg-blue-900/40 text-blue-400",
  "anomaly.resolve": "bg-amber-900/40 text-amber-400",
  default: "bg-slate-700 text-slate-400",
};

function formatAmount(n: unknown) {
  if (typeof n !== "number") return null;
  return n.toLocaleString("ru-RU", { minimumFractionDigits: 2 });
}

export default function ApprovalsPage() {
  const [approvals, setApprovals] = useState<ApprovalItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [comment, setComment] = useState("");
  const [toast, setToast] = useState<{ text: string; ok: boolean } | null>(
    null,
  );
  const [loading, setLoading] = useState(false);

  const selected = approvals.find((a) => a.id === selectedId) ?? null;

  const load = useCallback(() => {
    fetch(`${API}/api/approvals/pending`, { credentials: "include" })
      .then((r) => r.json())
      .then((data) => {
        const items: ApprovalItem[] = data.items ?? [];
        setApprovals(items);
        if (items.length > 0 && !selectedId) setSelectedId(items[0].id);
      })
      .catch(() => {});
  }, [selectedId]);

  useEffect(() => {
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  function showToast(text: string, ok: boolean) {
    setToast({ text, ok });
    setTimeout(() => setToast(null), 3000);
  }

  const decide = useCallback(
    async (id: string, approved: boolean) => {
      setLoading(true);
      try {
        const res = await fetch(`${API}/api/approvals/${id}/decide`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            status: approved ? "approved" : "rejected",
            comment: comment || null,
            decided_by: "user",
          }),
        });
        if (!res.ok) throw new Error(await res.text());
        setApprovals((prev) => {
          const next = prev.filter((a) => a.id !== id);
          setSelectedId(next[0]?.id ?? null);
          return next;
        });
        setComment("");
        showToast(approved ? "Утверждено" : "Отклонено", approved);
      } catch (e) {
        showToast(`Ошибка: ${String(e).slice(0, 60)}`, false);
      } finally {
        setLoading(false);
      }
    },
    [comment],
  );

  // Keyboard: j/k navigate, y approve, n reject
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      )
        return;
      const idx = approvals.findIndex((a) => a.id === selectedId);
      if (e.key === "j")
        setSelectedId(
          approvals[Math.min(idx + 1, approvals.length - 1)]?.id ?? null,
        );
      if (e.key === "k")
        setSelectedId(approvals[Math.max(idx - 1, 0)]?.id ?? null);
      if (e.key === "y" && selectedId) decide(selectedId, true);
      if (e.key === "n" && selectedId) decide(selectedId, false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approvals, selectedId, decide]);

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-6 py-4 border-b border-slate-700 flex items-center justify-between shrink-0">
        <div>
          <h1 className="text-xl font-semibold">Согласования</h1>
          {approvals.length > 0 && (
            <p className="text-xs text-slate-400 mt-0.5">
              {approvals.length} ожидают решения
            </p>
          )}
        </div>
        <div className="text-[11px] text-slate-400 flex gap-3">
          <span>
            <kbd className="px-1 border border-slate-600 rounded bg-slate-700">
              j/k
            </kbd>{" "}
            навигация
          </span>
          <span>
            <kbd className="px-1 border border-slate-600 rounded bg-green-900/40 text-green-400">
              y
            </kbd>{" "}
            утвердить
          </span>
          <span>
            <kbd className="px-1 border border-slate-600 rounded bg-red-900/40 text-red-400">
              n
            </kbd>{" "}
            отклонить
          </span>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div
          className={`mx-6 mt-3 px-4 py-2 rounded-lg text-sm shrink-0 ${
            toast.ok
              ? "bg-green-900/40 text-green-400 border border-green-700"
              : "bg-red-900/40 text-red-400 border border-red-700"
          }`}
        >
          {toast.text}
        </div>
      )}

      {/* Empty state */}
      {approvals.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-slate-400">
          <div className="text-center">
            <svg
              className="w-12 h-12 mx-auto mb-3 text-slate-300"
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
            <p className="text-sm font-medium">Нет ожидающих согласований</p>
            <p className="text-xs mt-1">Все задачи обработаны</p>
          </div>
        </div>
      ) : (
        <div className="flex-1 flex overflow-hidden">
          {/* Left: list */}
          <div className="w-72 border-r border-slate-700 overflow-y-auto shrink-0">
            {approvals.map((a) => {
              const colorClass =
                ACTION_COLORS[a.action_type] ?? ACTION_COLORS.default;
              const expiry = expiryInfo(a.expires_at);
              const showWarning =
                expiry !== null &&
                a.expires_at !== null &&
                new Date(a.expires_at).getTime() - Date.now() < 7_200_000 &&
                new Date(a.expires_at).getTime() - Date.now() >= 0;
              return (
                <button
                  key={a.id}
                  onClick={() => setSelectedId(a.id)}
                  className={`w-full text-left px-4 py-3 border-b border-slate-700/50 transition-colors ${
                    a.id === selectedId
                      ? "bg-blue-900/30 border-l-2 border-l-blue-500"
                      : "hover:bg-slate-700/50"
                  }`}
                >
                  <div className="flex items-center gap-1.5 flex-wrap">
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${colorClass}`}
                    >
                      {ACTION_LABELS[a.action_type] ?? a.action_type}
                    </span>
                    {showWarning && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded font-medium bg-amber-900/40 text-amber-400">
                        ⏰ истекает через {expiry!.label}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-slate-300 mt-1.5 truncate font-medium">
                    {(a.context?.invoice_number as string)
                      ? `№ ${a.context?.invoice_number}`
                      : ((a.context?.subject as string) ??
                        a.entity_id.slice(0, 8))}
                  </p>
                  {a.context?.total_amount != null && (
                    <p className="text-xs text-slate-500">
                      {formatAmount(a.context.total_amount)}{" "}
                      {(a.context.currency as string) ?? "RUB"}
                    </p>
                  )}
                  <p className="text-[11px] text-slate-400 mt-0.5">
                    {a.requested_by ?? "sveta"} ·{" "}
                    {new Date(a.created_at).toLocaleString("ru-RU", {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </p>
                </button>
              );
            })}
          </div>

          {/* Right: detail panel */}
          {selected && (
            <div className="flex-1 overflow-y-auto p-6">
              <div className="max-w-2xl">
                <span
                  className={`text-xs px-2 py-1 rounded font-medium ${
                    ACTION_COLORS[selected.action_type] ?? ACTION_COLORS.default
                  }`}
                >
                  {ACTION_LABELS[selected.action_type] ?? selected.action_type}
                </span>

                {/* Context details */}
                <div className="mt-4 bg-slate-800 rounded-lg border border-slate-700 p-4">
                  <h3 className="text-sm font-semibold mb-3 text-slate-300">
                    Детали запроса
                  </h3>
                  <ContextPanel
                    context={selected.context}
                    actionType={selected.action_type}
                  />
                </div>

                {/* Meta */}
                <div className="mt-3 text-xs text-slate-400 flex gap-4">
                  <span>
                    Запросил:{" "}
                    <strong className="text-slate-300">
                      {selected.requested_by ?? "sveta"}
                    </strong>
                  </span>
                  <span>
                    Создано:{" "}
                    <strong className="text-slate-300">
                      {new Date(selected.created_at).toLocaleString("ru-RU")}
                    </strong>
                  </span>
                </div>

                {/* Chain progress */}
                {selected.chain_root_id && selected.chain_order != null && (
                  <div className="mt-3 flex items-center gap-2 text-xs text-slate-400">
                    <span className="px-2 py-0.5 bg-slate-700 rounded font-medium text-slate-300">
                      Шаг {selected.chain_order + 1} цепочки согласования
                    </span>
                  </div>
                )}

                {/* Expiry + SLA */}
                {selected.expires_at &&
                  (() => {
                    const exp = expiryInfo(selected.expires_at);
                    const formatted = new Date(
                      selected.expires_at,
                    ).toLocaleString("ru-RU", {
                      day: "2-digit",
                      month: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                    });
                    return (
                      <>
                        <div className="mt-2 text-xs flex gap-2 items-center">
                          <span className="text-slate-400">Срок истекает:</span>
                          <strong className={exp?.cls ?? "text-slate-300"}>
                            {formatted}
                            {exp && ` — ${exp.label}`}
                          </strong>
                        </div>
                        <SlaBar
                          createdAt={selected.created_at}
                          expiresAt={selected.expires_at}
                        />
                      </>
                    );
                  })()}

                {/* Comment */}
                <div className="mt-4">
                  <textarea
                    value={comment}
                    onChange={(e) => setComment(e.target.value)}
                    placeholder="Комментарий (необязательно)..."
                    rows={2}
                    className="w-full text-sm bg-slate-800 border border-slate-600 text-slate-200 placeholder-slate-500 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                  />
                </div>

                {/* Decision buttons */}
                <div className="mt-3 flex gap-3">
                  <button
                    onClick={() => decide(selected.id, true)}
                    disabled={loading}
                    className="flex-1 py-2.5 bg-green-600 text-white rounded-lg text-sm font-medium hover:bg-green-700 disabled:opacity-50 flex items-center justify-center gap-2"
                  >
                    <kbd className="text-[10px] px-1 bg-green-500 rounded opacity-75">
                      Y
                    </kbd>
                    Утвердить
                  </button>
                  <button
                    onClick={() => decide(selected.id, false)}
                    disabled={loading}
                    className="flex-1 py-2.5 bg-red-500 text-white rounded-lg text-sm font-medium hover:bg-red-600 disabled:opacity-50 flex items-center justify-center gap-2"
                  >
                    <kbd className="text-[10px] px-1 bg-red-400 rounded opacity-75">
                      N
                    </kbd>
                    Отклонить
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Context panel ──────────────────────────────────────────────────────────

function ContextPanel({
  context,
  actionType,
}: {
  context: Record<string, unknown> | null;
  actionType: string;
}) {
  if (!context)
    return <p className="text-sm text-slate-400">Нет данных контекста</p>;

  if (actionType === "invoice.approve" || actionType === "invoice.reject") {
    return (
      <div className="space-y-2 text-sm">
        {!!context.invoice_number && (
          <Row label="Номер счёта" value={`№ ${context.invoice_number}`} />
        )}
        {!!context.invoice_date && (
          <Row
            label="Дата"
            value={new Date(context.invoice_date as string).toLocaleDateString(
              "ru-RU",
            )}
          />
        )}
        {context.total_amount != null && (
          <Row
            label="Сумма"
            value={`${formatAmount(context.total_amount as number)} ${(context.currency as string) ?? "RUB"}`}
            highlight
          />
        )}
        {!!context.supplier_name && (
          <Row label="Поставщик" value={context.supplier_name as string} />
        )}
        {!!context.document_id && (
          <Row
            label="Документ"
            value={
              <a
                href={`/documents/${context.document_id}/review`}
                className="text-blue-400 underline hover:text-blue-300"
                target="_blank"
                rel="noreferrer"
              >
                Открыть документ →
              </a>
            }
          />
        )}
      </div>
    );
  }

  if (actionType === "email.send") {
    return (
      <div className="space-y-2 text-sm">
        {!!context.to && <Row label="Кому" value={context.to as string} />}
        {!!context.subject && (
          <Row label="Тема" value={context.subject as string} />
        )}
        {!!context.body && (
          <div>
            <p className="text-xs text-slate-400 mb-1">Текст письма</p>
            <pre className="text-xs bg-slate-900 text-slate-300 rounded border border-slate-600 p-2 whitespace-pre-wrap max-h-40 overflow-y-auto">
              {context.body as string}
            </pre>
          </div>
        )}
      </div>
    );
  }

  // Generic fallback
  return (
    <div className="space-y-1.5 text-sm">
      {Object.entries(context).map(([k, v]) => (
        <Row key={k} label={k} value={String(v)} />
      ))}
    </div>
  );
}

function Row({
  label,
  value,
  highlight,
}: {
  label: string;
  value: React.ReactNode;
  highlight?: boolean;
}) {
  return (
    <div className="flex items-start gap-2">
      <span className="w-28 shrink-0 text-slate-400 text-xs pt-0.5">
        {label}
      </span>
      <span
        className={`flex-1 ${highlight ? "font-semibold text-slate-100" : "text-slate-300"}`}
      >
        {value}
      </span>
    </div>
  );
}
