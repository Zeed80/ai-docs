"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";

const API = getApiBaseUrl();

interface Anomaly {
  id: string;
  anomaly_type: string;
  severity: string;
  status: string;
  entity_type: string;
  entity_id: string;
  title: string;
  description: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  created_at: string;
}

const SEVERITY_STYLES: Record<string, string> = {
  critical: "bg-red-950/40 text-red-300 border-red-700/40",
  warning: "bg-amber-950/40 text-amber-300 border-amber-700/40",
  info: "bg-blue-950/40 text-blue-300 border-blue-700/40",
};

const TYPE_LABELS: Record<string, string> = {
  duplicate: "Дубликат",
  new_supplier: "Новый поставщик",
  requisite_change: "Смена реквизитов",
  price_spike: "Скачок цены",
  unknown_item: "Неизвестная позиция",
  invoice_email_mismatch: "Расхождение с письмом",
};

export default function AnomalyDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = params.id as string;

  const [anomaly, setAnomaly] = useState<Anomaly | null>(null);
  const [loading, setLoading] = useState(true);
  const [resolving, setResolving] = useState(false);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/anomalies/${id}`)
      .then((r) => {
        if (r.status === 404) {
          setNotFound(true);
          return null;
        }
        return r.json();
      })
      .then((d) => {
        if (d) setAnomaly(d);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [id]);

  const resolve = async (resolution: "resolved" | "false_positive") => {
    setResolving(true);
    await fetch(`${API}/api/anomalies/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolution }),
    }).catch(() => {});
    setResolving(false);
    router.push("/anomalies");
  };

  if (loading) return <div className="p-6 text-slate-400">Загрузка...</div>;

  if (notFound || !anomaly) {
    return (
      <div className="p-6 max-w-2xl mx-auto">
        <div className="text-slate-400 mb-4">Аномалия не найдена.</div>
        <Link
          href="/anomalies"
          className="text-blue-400 hover:underline text-sm"
        >
          ← Все аномалии
        </Link>
      </div>
    );
  }

  return (
    <div className="p-6 max-w-2xl mx-auto">
      <div className="mb-4">
        <Link
          href="/anomalies"
          className="text-blue-400 hover:underline text-sm"
        >
          ← Все аномалии
        </Link>
      </div>

      <div
        className={`border rounded-lg p-5 ${SEVERITY_STYLES[anomaly.severity] ?? "bg-slate-800 border-slate-700"}`}
      >
        <div className="flex items-center gap-2 mb-2">
          <span className="text-xs font-semibold uppercase tracking-wide">
            {TYPE_LABELS[anomaly.anomaly_type] ?? anomaly.anomaly_type}
          </span>
          <span
            className={`text-xs px-2 py-0.5 rounded ${
              anomaly.status === "open"
                ? "bg-white/10"
                : "bg-green-900/40 text-green-400"
            }`}
          >
            {anomaly.status === "open"
              ? "Открыта"
              : anomaly.status === "resolved"
                ? "Решена"
                : "Ложная"}
          </span>
        </div>

        <h1 className="text-base font-bold mb-2">{anomaly.title}</h1>

        {anomaly.description && (
          <p className="text-sm opacity-80 mb-3">{anomaly.description}</p>
        )}

        <div className="text-xs opacity-50 mb-4">
          Создана: {new Date(anomaly.created_at).toLocaleString("ru-RU")}
        </div>

        {anomaly.entity_type && anomaly.entity_id && (
          <div className="mb-4">
            <Link
              href={`/${anomaly.entity_type}s/${anomaly.entity_id}`}
              className="text-sm text-blue-400 hover:underline"
            >
              Открыть связанный {anomaly.entity_type} →
            </Link>
          </div>
        )}

        {anomaly.status === "open" && (
          <div className="flex gap-3 mt-4">
            <button
              onClick={() => resolve("resolved")}
              disabled={resolving}
              className="px-4 py-2 text-sm font-medium bg-green-700 hover:bg-green-600 text-white rounded transition-colors disabled:opacity-50"
            >
              Решить
            </button>
            <button
              onClick={() => resolve("false_positive")}
              disabled={resolving}
              className="px-4 py-2 text-sm font-medium bg-slate-700 hover:bg-slate-600 text-slate-200 rounded transition-colors disabled:opacity-50"
            >
              Ложная
            </button>
          </div>
        )}

        {anomaly.status !== "open" && anomaly.resolved_at && (
          <div className="text-xs opacity-50 mt-2">
            Решена: {new Date(anomaly.resolved_at).toLocaleString("ru-RU")}
          </div>
        )}
      </div>
    </div>
  );
}
