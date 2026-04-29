"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface SupplierFull {
  id: string;
  name: string;
  inn: string | null;
  kpp: string | null;
  address: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  bank_name: string | null;
  bank_bik: string | null;
  bank_account: string | null;
  profile: {
    total_invoices: number;
    total_amount: number;
    trust_score: number | null;
    last_invoice_date: string | null;
    notes: string | null;
  } | null;
  recent_invoices_count: number;
  open_invoices_amount: number;
}

interface TrustScore {
  trust_score: number;
  breakdown: {
    factor: string;
    weight: number;
    score: number;
    detail: string | null;
  }[];
  recommendation: string | null;
}

interface PriceHistoryItem {
  description: string;
  current_price: number | null;
  avg_price: number | null;
  trend: string | null;
  points: { date: string; price: number }[];
}

interface Alert {
  id: string;
  alert_type: string;
  severity: string;
  message: string;
}

interface RequisiteCheck {
  field: string;
  status: string;
  message: string | null;
}

export default function SupplierProfilePage() {
  const params = useParams();
  const id = params.id as string;
  const router = useRouter();

  const [supplier, setSupplier] = useState<SupplierFull | null>(null);
  const [trust, setTrust] = useState<TrustScore | null>(null);
  const [prices, setPrices] = useState<PriceHistoryItem[]>([]);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [requisites, setRequisites] = useState<RequisiteCheck[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    setLoading(true);

    Promise.all([
      fetch(`${API}/api/suppliers/${id}`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/trust-score`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/price-history`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/alerts`).then((r) => r.json()),
      fetch(`${API}/api/suppliers/${id}/check-requisites`, {
        method: "POST",
      }).then((r) => r.json()),
    ])
      .then(([s, t, p, a, req]) => {
        setSupplier(s);
        setTrust(t);
        setPrices(p.items ?? []);
        setAlerts(a.alerts ?? []);
        setRequisites(req.checks ?? []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) return <div className="p-6 text-slate-400">Загрузка...</div>;
  if (!supplier)
    return <div className="p-6 text-slate-400">Поставщик не найден</div>;

  const trustColor =
    (trust?.trust_score ?? 0) >= 0.8
      ? "text-green-600"
      : (trust?.trust_score ?? 0) >= 0.5
        ? "text-amber-600"
        : "text-red-600";

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <button
        onClick={() => router.back()}
        className="text-sm text-slate-500 hover:text-slate-700 mb-4"
      >
        &larr; Назад
      </button>

      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold">{supplier.name}</h1>
          <div className="text-sm text-slate-500 mt-1">
            ИНН {supplier.inn ?? "—"} / КПП {supplier.kpp ?? "—"}
          </div>
          {supplier.address && (
            <div className="text-sm text-slate-400 mt-0.5">
              {supplier.address}
            </div>
          )}
        </div>
        {trust && (
          <div className="text-right">
            <div className={`text-2xl font-bold ${trustColor}`}>
              {(trust.trust_score * 100).toFixed(0)}%
            </div>
            <div className="text-xs text-slate-500">{trust.recommendation}</div>
          </div>
        )}
      </div>

      {/* Alerts */}
      {alerts.length > 0 && (
        <div className="mb-6 space-y-2">
          {alerts.map((a) => (
            <div
              key={a.id}
              className={`px-4 py-2 rounded text-sm ${
                a.severity === "error"
                  ? "bg-red-50 text-red-700 border border-red-200"
                  : "bg-amber-50 text-amber-700 border border-amber-200"
              }`}
            >
              {a.message}
            </div>
          ))}
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        <StatCard
          label="Счетов"
          value={supplier.profile?.total_invoices ?? 0}
        />
        <StatCard
          label="Общая сумма"
          value={`${((supplier.profile?.total_amount ?? 0) / 1000).toFixed(0)}K`}
        />
        <StatCard label="Открытых" value={supplier.recent_invoices_count} />
        <StatCard
          label="Открытая сумма"
          value={`${(supplier.open_invoices_amount / 1000).toFixed(0)}K`}
        />
      </div>

      <div className="grid grid-cols-2 gap-6">
        {/* Trust Score Breakdown */}
        {trust && (
          <div className="bg-white border rounded-lg p-4">
            <h3 className="font-bold text-sm mb-3">Trust Score</h3>
            <div className="space-y-2">
              {trust.breakdown.map((b) => (
                <div key={b.factor}>
                  <div className="flex justify-between text-xs text-slate-600">
                    <span>{b.detail}</span>
                    <span className="font-mono">
                      {(b.score * 100).toFixed(0)}%
                    </span>
                  </div>
                  <div className="h-1.5 bg-slate-100 rounded-full mt-1">
                    <div
                      className="h-1.5 bg-blue-500 rounded-full"
                      style={{ width: `${b.score * 100}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Requisites */}
        <div className="bg-white border rounded-lg p-4">
          <h3 className="font-bold text-sm mb-3">Реквизиты</h3>
          <div className="space-y-1.5 text-sm">
            {requisites.map((r) => (
              <div key={r.field} className="flex items-center gap-2">
                <span
                  className={`w-2 h-2 rounded-full ${
                    r.status === "ok"
                      ? "bg-green-500"
                      : r.status === "warning"
                        ? "bg-amber-500"
                        : r.status === "error"
                          ? "bg-red-500"
                          : "bg-slate-300"
                  }`}
                />
                <span className="text-slate-600">
                  {r.field}: {r.message ?? "OK"}
                </span>
              </div>
            ))}
          </div>
          <div className="mt-3 text-xs text-slate-400">
            Email: {supplier.contact_email ?? "—"} | Тел:{" "}
            {supplier.contact_phone ?? "—"}
          </div>
        </div>
      </div>

      {/* Price History */}
      {prices.length > 0 && (
        <div className="mt-6 bg-white border rounded-lg p-4">
          <h3 className="font-bold text-sm mb-3">
            История цен ({prices.length} позиций)
          </h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-xs text-slate-500 uppercase">
                <tr>
                  <th className="text-left px-3 py-1.5">Позиция</th>
                  <th className="text-right px-3 py-1.5">Текущая</th>
                  <th className="text-right px-3 py-1.5">Средняя</th>
                  <th className="text-center px-3 py-1.5">Тренд</th>
                  <th className="text-right px-3 py-1.5">Точек</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {prices.map((item) => (
                  <tr key={item.description}>
                    <td className="px-3 py-2">{item.description}</td>
                    <td className="px-3 py-2 text-right font-mono">
                      {item.current_price?.toLocaleString("ru-RU") ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-slate-500">
                      {item.avg_price?.toLocaleString("ru-RU") ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-center">
                      {item.trend === "up" && (
                        <span className="text-red-500 text-xs">
                          &#9650; Рост
                        </span>
                      )}
                      {item.trend === "down" && (
                        <span className="text-green-500 text-xs">
                          &#9660; Снижение
                        </span>
                      )}
                      {item.trend === "stable" && (
                        <span className="text-slate-400 text-xs">
                          &#9654; Стабильно
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right text-slate-400">
                      {item.points.length}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="bg-white border rounded-lg p-3 text-center">
      <div className="text-lg font-bold">{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}
