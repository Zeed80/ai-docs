"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";

interface SimilarResult {
  id: string;
  score: number;
  entity_type: string;
  snippet?: string | null;
}

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
  const [explainData, setExplainData] = useState<{
    explanation: string;
    suggested_actions: string[];
  } | null>(null);
  const [explaining, setExplaining] = useState(false);
  const [similarAnomalies, setSimilarAnomalies] = useState<SimilarResult[]>([]);

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

  useEffect(() => {
    fetch(`${API}/api/search/similar/anomaly/${id}?limit=4`, {
      credentials: "include",
    })
      .then((r) => (r.ok ? r.json() : { results: [] }))
      .then((d) => setSimilarAnomalies(d.results ?? []))
      .catch(() => {});
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

  const explain = async () => {
    setExplaining(true);
    try {
      const r = await fetch(`${API}/api/anomalies/${id}/explain`);
      if (r.ok) {
        const d = (await r.json()) as {
          explanation: string;
          suggested_actions: string[];
        };
        setExplainData(d);
      }
    } catch {
      // non-critical
    } finally {
      setExplaining(false);
    }
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

        <div className="flex gap-3 mt-4 flex-wrap">
          {anomaly.status === "open" && (
            <>
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
            </>
          )}
          <button
            onClick={() => void explain()}
            disabled={explaining}
            className="px-4 py-2 text-sm font-medium bg-blue-800 hover:bg-blue-700 text-white rounded transition-colors disabled:opacity-50"
          >
            {explaining ? "Анализирую…" : "🤖 Объяснить"}
          </button>
        </div>

        {explainData && (
          <div className="mt-4 p-3 bg-slate-900/60 border border-slate-600 rounded text-sm text-slate-200 space-y-2">
            <p className="whitespace-pre-wrap">{explainData.explanation}</p>
            {explainData.suggested_actions.length > 0 && (
              <div>
                <div className="text-xs text-slate-400 font-medium mb-1">
                  Рекомендуемые действия:
                </div>
                <ul className="list-disc list-inside space-y-0.5 text-slate-300">
                  {explainData.suggested_actions.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        {anomaly.status !== "open" && anomaly.resolved_at && (
          <div className="text-xs opacity-50 mt-2">
            Решена: {new Date(anomaly.resolved_at).toLocaleString("ru-RU")}
          </div>
        )}
      </div>

      {similarAnomalies.length > 0 && (
        <div className="mt-4 bg-slate-900 border border-slate-700 rounded-lg p-4">
          <h3 className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-3">
            Похожие аномалии
          </h3>
          <ul className="space-y-2">
            {similarAnomalies.map((s) => (
              <li key={s.id}>
                <Link
                  href={`/anomalies/${s.id}`}
                  className="flex items-center justify-between gap-2 group"
                >
                  <span className="text-sm text-blue-400 group-hover:underline truncate">
                    {s.snippet ?? s.id.slice(0, 16) + "…"}
                  </span>
                  <span className="shrink-0 text-xs text-slate-500 font-mono">
                    {(s.score * 100).toFixed(0)}%
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
