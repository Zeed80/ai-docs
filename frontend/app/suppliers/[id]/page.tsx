"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = getApiBaseUrl();

interface SupplierFull {
  id: string;
  name: string;
  inn: string | null;
  kpp: string | null;
  ogrn: string | null;
  address: string | null;
  contact_email: string | null;
  contact_phone: string | null;
  bank_name: string | null;
  bank_bik: string | null;
  bank_account: string | null;
  corr_account: string | null;
  user_notes: string | null;
  user_rating: number | null;
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

function StarRating({
  value,
  onChange,
  readonly,
}: {
  value: number;
  onChange?: (v: number) => void;
  readonly?: boolean;
}) {
  const [hover, setHover] = useState(0);
  return (
    <span className="flex gap-0.5">
      {[1, 2, 3, 4, 5].map((star) => (
        <button
          key={star}
          type="button"
          disabled={readonly}
          onClick={() => onChange?.(star === value ? 0 : star)}
          onMouseEnter={() => !readonly && setHover(star)}
          onMouseLeave={() => !readonly && setHover(0)}
          className={`text-xl leading-none transition-colors ${readonly ? "cursor-default" : "cursor-pointer"} ${
            star <= (hover || value) ? "text-yellow-400" : "text-slate-600"
          }`}
        >
          ★
        </button>
      ))}
    </span>
  );
}

function Field({
  label,
  value,
  editing,
  onChange,
  multiline,
  className,
}: {
  label: string;
  value: string;
  editing: boolean;
  onChange: (v: string) => void;
  multiline?: boolean;
  className?: string;
}) {
  if (!editing) {
    return (
      <div className={className}>
        <span className="text-xs text-slate-500">{label}</span>
        <p className="text-sm text-slate-200 mt-0.5">{value || "—"}</p>
      </div>
    );
  }
  return (
    <div className={className}>
      <label className="block text-xs text-slate-400 mb-1">{label}</label>
      {multiline ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={3}
          className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500 resize-none"
        />
      ) : (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 rounded text-slate-200 focus:outline-none focus:border-blue-500"
        />
      )}
    </div>
  );
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

  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState<Partial<SupplierFull>>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  function load() {
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
  }

  useEffect(() => {
    if (!id) return;
    load();
  }, [id]);

  function startEdit() {
    if (!supplier) return;
    setEditForm({
      name: supplier.name,
      inn: supplier.inn ?? "",
      kpp: supplier.kpp ?? "",
      ogrn: supplier.ogrn ?? "",
      address: supplier.address ?? "",
      contact_email: supplier.contact_email ?? "",
      contact_phone: supplier.contact_phone ?? "",
      bank_name: supplier.bank_name ?? "",
      bank_bik: supplier.bank_bik ?? "",
      bank_account: supplier.bank_account ?? "",
      corr_account: supplier.corr_account ?? "",
      user_notes: supplier.user_notes ?? "",
      user_rating: supplier.user_rating ?? 0,
    });
    setSaveError("");
    setEditing(true);
  }

  async function saveEdit() {
    setSaving(true);
    setSaveError("");
    try {
      const body: Record<string, unknown> = {};
      const fields = [
        "name",
        "inn",
        "kpp",
        "ogrn",
        "address",
        "contact_email",
        "contact_phone",
        "bank_name",
        "bank_bik",
        "bank_account",
        "corr_account",
        "user_notes",
      ] as const;
      for (const f of fields) {
        const v = editForm[f as keyof typeof editForm];
        body[f] = typeof v === "string" && v.trim() === "" ? null : (v ?? null);
      }
      body.user_rating = (editForm.user_rating as number) || null;

      const resp = await fetch(`${API}/api/suppliers/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        setSaveError("Ошибка сохранения");
        return;
      }
      setEditing(false);
      load();
    } finally {
      setSaving(false);
    }
  }

  if (loading)
    return <div className="p-6 text-slate-400 text-sm">Загрузка...</div>;
  if (!supplier)
    return (
      <div className="p-6 text-slate-400 text-sm">Поставщик не найден</div>
    );

  const trustColor =
    (trust?.trust_score ?? 0) >= 0.8
      ? "text-green-400"
      : (trust?.trust_score ?? 0) >= 0.5
        ? "text-amber-400"
        : "text-red-400";

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <button
        onClick={() => router.back()}
        className="text-sm text-slate-500 hover:text-slate-300 mb-4 block"
      >
        ← Назад
      </button>

      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div className="flex-1 min-w-0">
          {editing ? (
            <input
              type="text"
              value={(editForm.name as string) ?? ""}
              onChange={(e) =>
                setEditForm((f) => ({ ...f, name: e.target.value }))
              }
              className="text-xl font-bold bg-slate-700 border border-slate-600 rounded px-3 py-1 text-slate-100 focus:outline-none focus:border-blue-500 w-full max-w-lg"
            />
          ) : (
            <h1 className="text-xl font-bold text-slate-100">
              {supplier.name}
            </h1>
          )}
          <div className="text-sm text-slate-500 mt-1">
            ИНН {supplier.inn ?? "—"} / КПП {supplier.kpp ?? "—"}
          </div>
          {supplier.address && !editing && (
            <div className="text-sm text-slate-400 mt-0.5">
              {supplier.address}
            </div>
          )}
        </div>
        <div className="flex items-start gap-4 ml-4 shrink-0">
          {trust && !editing && (
            <div className="text-right">
              <div className={`text-2xl font-bold ${trustColor}`}>
                {(trust.trust_score * 100).toFixed(0)}%
              </div>
              <div className="text-xs text-slate-500">
                {trust.recommendation}
              </div>
            </div>
          )}
          {editing ? (
            <div className="flex gap-2">
              <button
                onClick={() => setEditing(false)}
                className="px-3 py-1.5 text-xs border border-slate-600 text-slate-400 hover:text-slate-200 rounded transition-colors"
              >
                Отмена
              </button>
              <button
                onClick={saveEdit}
                disabled={saving}
                className="px-4 py-1.5 text-xs bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white rounded transition-colors"
              >
                {saving ? "Сохраняю..." : "Сохранить"}
              </button>
            </div>
          ) : (
            <button
              onClick={startEdit}
              className="px-3 py-1.5 text-xs border border-slate-600 text-slate-400 hover:text-slate-200 hover:border-slate-500 rounded transition-colors"
            >
              Редактировать
            </button>
          )}
        </div>
      </div>

      {saveError && <p className="text-red-400 text-xs mb-4">{saveError}</p>}

      {/* Alerts */}
      {alerts.length > 0 && !editing && (
        <div className="mb-6 space-y-2">
          {alerts.map((a) => (
            <div
              key={a.id}
              className={`px-4 py-2 rounded text-sm ${
                a.severity === "error"
                  ? "bg-red-500/10 text-red-400 border border-red-500/30"
                  : "bg-amber-500/10 text-amber-400 border border-amber-500/30"
              }`}
            >
              {a.message}
            </div>
          ))}
        </div>
      )}

      {/* Edit form */}
      {editing && (
        <div className="bg-slate-800 border border-slate-700 rounded-lg p-5 mb-6 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <Field
              label="ИНН"
              value={(editForm.inn as string) ?? ""}
              editing
              onChange={(v) => setEditForm((f) => ({ ...f, inn: v }))}
            />
            <Field
              label="КПП"
              value={(editForm.kpp as string) ?? ""}
              editing
              onChange={(v) => setEditForm((f) => ({ ...f, kpp: v }))}
            />
            <Field
              label="ОГРН"
              value={(editForm.ogrn as string) ?? ""}
              editing
              onChange={(v) => setEditForm((f) => ({ ...f, ogrn: v }))}
            />
            <Field
              label="Email"
              value={(editForm.contact_email as string) ?? ""}
              editing
              onChange={(v) => setEditForm((f) => ({ ...f, contact_email: v }))}
            />
            <Field
              label="Телефон"
              value={(editForm.contact_phone as string) ?? ""}
              editing
              onChange={(v) => setEditForm((f) => ({ ...f, contact_phone: v }))}
            />
            <Field
              label="Адрес"
              value={(editForm.address as string) ?? ""}
              editing
              onChange={(v) => setEditForm((f) => ({ ...f, address: v }))}
              className="col-span-2"
            />
          </div>

          <details className="group">
            <summary className="cursor-pointer text-xs text-slate-400 hover:text-slate-200 select-none">
              Банковские реквизиты
            </summary>
            <div className="grid grid-cols-2 gap-4 mt-3">
              <Field
                label="Банк"
                value={(editForm.bank_name as string) ?? ""}
                editing
                onChange={(v) => setEditForm((f) => ({ ...f, bank_name: v }))}
                className="col-span-2"
              />
              <Field
                label="БИК"
                value={(editForm.bank_bik as string) ?? ""}
                editing
                onChange={(v) => setEditForm((f) => ({ ...f, bank_bik: v }))}
              />
              <Field
                label="Р/с"
                value={(editForm.bank_account as string) ?? ""}
                editing
                onChange={(v) =>
                  setEditForm((f) => ({ ...f, bank_account: v }))
                }
              />
              <Field
                label="Корр. счёт"
                value={(editForm.corr_account as string) ?? ""}
                editing
                onChange={(v) =>
                  setEditForm((f) => ({ ...f, corr_account: v }))
                }
              />
            </div>
          </details>

          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Рейтинг поставщика
            </label>
            <StarRating
              value={(editForm.user_rating as number) ?? 0}
              onChange={(v) => setEditForm((f) => ({ ...f, user_rating: v }))}
            />
          </div>

          <Field
            label="Заметки"
            value={(editForm.user_notes as string) ?? ""}
            editing
            onChange={(v) => setEditForm((f) => ({ ...f, user_notes: v }))}
            multiline
          />
        </div>
      )}

      {/* User rating + notes (read mode) */}
      {!editing && (supplier.user_rating || supplier.user_notes) && (
        <div className="bg-slate-800 border border-slate-700 rounded-lg p-4 mb-6 space-y-2">
          {supplier.user_rating ? (
            <div className="flex items-center gap-3">
              <span className="text-xs text-slate-500">Оценка:</span>
              <StarRating value={supplier.user_rating} readonly />
            </div>
          ) : null}
          {supplier.user_notes ? (
            <div>
              <span className="text-xs text-slate-500 block mb-1">
                Заметки:
              </span>
              <p className="text-sm text-slate-300 whitespace-pre-wrap">
                {supplier.user_notes}
              </p>
            </div>
          ) : null}
        </div>
      )}

      {/* Stats */}
      {!editing && (
        <div className="grid grid-cols-4 gap-4 mb-6">
          <StatCard
            label="Счетов"
            value={supplier.profile?.total_invoices ?? 0}
          />
          <StatCard
            label="Общая сумма"
            value={`${((supplier.profile?.total_amount ?? 0) / 1000).toFixed(0)} K`}
          />
          <StatCard label="Открытых" value={supplier.recent_invoices_count} />
          <StatCard
            label="Открытая сумма"
            value={`${(supplier.open_invoices_amount / 1000).toFixed(0)} K`}
          />
        </div>
      )}

      {!editing && (
        <div className="grid grid-cols-2 gap-6">
          {/* Trust Score Breakdown */}
          {trust && (
            <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
              <h3 className="font-semibold text-sm text-slate-200 mb-3">
                Trust Score
              </h3>
              <div className="space-y-2">
                {trust.breakdown.map((b) => (
                  <div key={b.factor}>
                    <div className="flex justify-between text-xs text-slate-400">
                      <span>{b.detail}</span>
                      <span className="font-mono">
                        {(b.score * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div className="h-1.5 bg-slate-700 rounded-full mt-1">
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
          <div className="bg-slate-800 border border-slate-700 rounded-lg p-4">
            <h3 className="font-semibold text-sm text-slate-200 mb-3">
              Реквизиты
            </h3>
            <div className="space-y-1.5 text-sm">
              {requisites.map((r) => (
                <div key={r.field} className="flex items-center gap-2">
                  <span
                    className={`w-2 h-2 rounded-full shrink-0 ${
                      r.status === "ok"
                        ? "bg-green-500"
                        : r.status === "warning"
                          ? "bg-amber-500"
                          : r.status === "error"
                            ? "bg-red-500"
                            : "bg-slate-500"
                    }`}
                  />
                  <span className="text-slate-400 text-xs">
                    {r.field}: {r.message ?? "OK"}
                  </span>
                </div>
              ))}
            </div>
            <div className="mt-3 text-xs text-slate-500">
              Email: {supplier.contact_email ?? "—"} | Тел:{" "}
              {supplier.contact_phone ?? "—"}
            </div>
          </div>
        </div>
      )}

      {/* Price History */}
      {!editing && prices.length > 0 && (
        <div className="mt-6 bg-slate-800 border border-slate-700 rounded-lg p-4">
          <h3 className="font-semibold text-sm text-slate-200 mb-3">
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
              <tbody className="divide-y divide-slate-700">
                {prices.map((item) => (
                  <tr key={item.description}>
                    <td className="px-3 py-2 text-slate-200">
                      {item.description}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-slate-200">
                      {item.current_price?.toLocaleString("ru-RU") ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-right font-mono text-slate-400">
                      {item.avg_price?.toLocaleString("ru-RU") ?? "—"}
                    </td>
                    <td className="px-3 py-2 text-center">
                      {item.trend === "up" && (
                        <span className="text-red-400 text-xs">▲ Рост</span>
                      )}
                      {item.trend === "down" && (
                        <span className="text-green-400 text-xs">
                          ▼ Снижение
                        </span>
                      )}
                      {item.trend === "stable" && (
                        <span className="text-slate-400 text-xs">
                          ▶ Стабильно
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2 text-right text-slate-500">
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
    <div className="bg-slate-800 border border-slate-700 rounded-lg p-3 text-center">
      <div className="text-lg font-bold text-slate-100">{value}</div>
      <div className="text-xs text-slate-500">{label}</div>
    </div>
  );
}
