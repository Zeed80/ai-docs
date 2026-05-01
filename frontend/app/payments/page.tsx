"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface PaymentSchedule {
  id: string;
  invoice_id: string;
  payment_number: number;
  due_date: string;
  amount: number;
  currency: string;
  status: string;
  payment_method?: string;
  paid_at?: string;
  paid_amount?: number;
  reference?: string;
  notes?: string;
  created_at: string;
}

const STATUS_LABELS: Record<string, string> = {
  scheduled: "Запланирован",
  paid: "Оплачен",
  overdue: "Просрочен",
  partial: "Частично",
  cancelled: "Отменён",
};

const STATUS_COLORS: Record<string, string> = {
  scheduled: "bg-blue-900 text-blue-200",
  paid: "bg-green-900 text-green-200",
  overdue: "bg-red-900 text-red-300",
  partial: "bg-yellow-900 text-yellow-200",
  cancelled: "bg-slate-700 text-slate-400",
};

function getDueDateColor(dueDate: string, status: string) {
  if (status === "paid" || status === "cancelled") return "text-slate-500";
  const now = new Date();
  const due = new Date(dueDate);
  const diffDays = Math.ceil(
    (due.getTime() - now.getTime()) / (1000 * 60 * 60 * 24),
  );
  if (diffDays < 0) return "text-red-400 font-semibold";
  if (diffDays <= 3) return "text-yellow-400 font-semibold";
  return "text-slate-300";
}

interface MarkPaidModalProps {
  schedule: PaymentSchedule;
  onClose: () => void;
  onPaid: () => void;
}

function MarkPaidModal({ schedule, onClose, onPaid }: MarkPaidModalProps) {
  const [paidAmount, setPaidAmount] = useState(String(schedule.amount));
  const [reference, setReference] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    try {
      const res = await fetch(
        `${API}/api/payment-schedules/${schedule.id}/mark-paid`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            paid_amount: Number(paidAmount),
            reference: reference || null,
          }),
        },
      );
      if (!res.ok) {
        const d = await res.json();
        setError(d.detail ?? "Ошибка");
        return;
      }
      onPaid();
    } catch {
      setError("Ошибка сети");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="bg-slate-800 rounded-lg p-6 w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-slate-100 mb-4">
          Отметить оплаченным
        </h2>
        {error && <p className="text-red-400 text-sm mb-3">{error}</p>}
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Сумма оплаты
            </label>
            <div className="flex gap-2">
              <input
                type="number"
                value={paidAmount}
                onChange={(e) => setPaidAmount(e.target.value)}
                step="0.01"
                className="flex-1 px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
              />
              <span className="px-3 py-2 bg-slate-700 rounded text-sm text-slate-400 border border-slate-600">
                {schedule.currency}
              </span>
            </div>
          </div>
          <div>
            <label className="block text-xs text-slate-400 mb-1">
              № платёжного поручения
            </label>
            <input
              value={reference}
              onChange={(e) => setReference(e.target.value)}
              placeholder="п/п №123 от 01.01.2026"
              className="w-full px-3 py-2 bg-slate-700 rounded text-sm text-slate-100 border border-slate-600 focus:outline-none focus:border-indigo-500"
            />
          </div>
          <div className="flex gap-3 pt-2">
            <button
              type="submit"
              disabled={loading}
              className="flex-1 py-2 bg-green-700 text-white rounded text-sm font-medium hover:bg-green-600 disabled:opacity-50"
            >
              {loading ? "Сохранение..." : "Подтвердить оплату"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-slate-400 hover:text-slate-200 text-sm"
            >
              Отмена
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function PaymentsPage() {
  const [schedules, setSchedules] = useState<PaymentSchedule[]>([]);
  const [loading, setLoading] = useState(true);
  const [tab, setTab] = useState<"upcoming" | "overdue" | "all">("upcoming");
  const [markingPaid, setMarkingPaid] = useState<PaymentSchedule | null>(null);

  async function loadSchedules() {
    setLoading(true);
    try {
      let url = "";
      if (tab === "upcoming")
        url = `${API}/api/payment-schedules/upcoming?days=30`;
      else if (tab === "overdue") url = `${API}/api/payment-schedules/overdue`;
      else url = `${API}/api/payment-schedules`;
      const res = await fetch(url);
      const data = await res.json();
      setSchedules(data.items ?? []);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadSchedules();
  }, [tab]);

  function formatDate(d: string) {
    return new Date(d).toLocaleDateString("ru-RU", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
    });
  }

  function formatAmount(amount: number, currency: string) {
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency,
      maximumFractionDigits: 2,
    }).format(amount);
  }

  const overduCount = schedules.filter(
    (s) =>
      s.status !== "paid" &&
      s.status !== "cancelled" &&
      new Date(s.due_date) < new Date(),
  ).length;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-slate-100">
          Платёжный календарь
        </h1>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 bg-slate-800 rounded-lg p-1 w-fit">
        {(
          [
            {
              key: "upcoming",
              label: "Предстоящие (30 дней)",
              badge: null as number | null,
            },
            {
              key: "overdue",
              label: "Просроченные",
              badge: overduCount > 0 && tab !== "overdue" ? overduCount : null,
            },
            { key: "all", label: "Все", badge: null as number | null },
          ] as const
        ).map(({ key, label, badge }) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`px-4 py-1.5 rounded text-sm font-medium transition-colors relative ${
              tab === key
                ? "bg-indigo-600 text-white"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {label}
            {badge != null && badge > 0 ? (
              <span className="ml-2 px-1.5 py-0.5 bg-red-600 text-white rounded-full text-xs">
                {badge}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      {loading ? (
        <p className="text-slate-400 text-sm">Загрузка...</p>
      ) : schedules.length === 0 ? (
        <div className="text-center py-16 text-slate-500">
          <p className="text-4xl mb-3">📅</p>
          <p className="text-sm">
            {tab === "overdue"
              ? "Просроченных платежей нет"
              : tab === "upcoming"
                ? "Предстоящих платежей нет"
                : "Платежей нет"}
          </p>
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-700">
                <th className="text-left py-2 pr-4 text-xs font-medium text-slate-500 uppercase tracking-wider">
                  Срок оплаты
                </th>
                <th className="text-left py-2 pr-4 text-xs font-medium text-slate-500 uppercase tracking-wider">
                  Счёт
                </th>
                <th className="text-right py-2 pr-4 text-xs font-medium text-slate-500 uppercase tracking-wider">
                  Сумма
                </th>
                <th className="text-left py-2 pr-4 text-xs font-medium text-slate-500 uppercase tracking-wider">
                  Статус
                </th>
                <th className="text-left py-2 pr-4 text-xs font-medium text-slate-500 uppercase tracking-wider">
                  Реквизиты
                </th>
                <th className="py-2"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {schedules.map((s) => (
                <tr key={s.id} className="hover:bg-slate-800/50">
                  <td
                    className={`py-3 pr-4 ${getDueDateColor(s.due_date, s.status)}`}
                  >
                    {formatDate(s.due_date)}
                  </td>
                  <td className="py-3 pr-4 text-slate-400 text-xs font-mono">
                    {s.invoice_id.slice(0, 8)}…
                  </td>
                  <td className="py-3 pr-4 text-right font-medium text-slate-100">
                    {formatAmount(s.amount, s.currency)}
                    {s.paid_amount && s.paid_amount !== s.amount && (
                      <div className="text-xs text-green-400">
                        оплачено {formatAmount(s.paid_amount, s.currency)}
                      </div>
                    )}
                  </td>
                  <td className="py-3 pr-4">
                    <span
                      className={`px-2 py-0.5 rounded text-xs font-medium ${STATUS_COLORS[s.status] ?? "bg-slate-700 text-slate-300"}`}
                    >
                      {STATUS_LABELS[s.status] ?? s.status}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-xs text-slate-500">
                    {s.reference || s.payment_method || "—"}
                    {s.paid_at && (
                      <div className="text-slate-600">
                        {formatDate(s.paid_at)}
                      </div>
                    )}
                  </td>
                  <td className="py-3">
                    {s.status !== "paid" && s.status !== "cancelled" && (
                      <button
                        onClick={() => setMarkingPaid(s)}
                        className="px-3 py-1 text-xs bg-green-900 text-green-200 rounded hover:bg-green-800"
                      >
                        Оплачен
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {markingPaid && (
        <MarkPaidModal
          schedule={markingPaid}
          onClose={() => setMarkingPaid(null)}
          onPaid={() => {
            setMarkingPaid(null);
            loadSchedules();
          }}
        />
      )}
    </div>
  );
}
