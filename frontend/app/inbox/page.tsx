"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getApiBaseUrl, getWebSocketBaseUrl } from "@/lib/api-base";
import { apiFetch } from "@/lib/auth";

const API = getApiBaseUrl();

/**
 * Ф7.1 — one feed of things waiting for a person.
 *
 * Mail used to live on a separate screen the mobile app did not even link to,
 * so checking "нужно ли от меня что-то" meant visiting two places. The merge
 * happens server-side (/api/inbox): only the server can order three sources by
 * time and apply personal-mailbox visibility while doing it.
 */
interface FeedItem {
  id: string;
  kind: "email" | "document" | "anomaly";
  title: string;
  subtitle: string | null;
  at: string | null;
  url: string;
  unread: boolean;
  severity: string | null;
  badge: string | null;
}

const KIND_FILTERS = [
  { key: "all", label: "Всё" },
  { key: "email", label: "Письма" },
  { key: "document", label: "Документы" },
  { key: "anomaly", label: "Аномалии" },
] as const;

const KIND_ICON: Record<FeedItem["kind"], string> = {
  email: "✉",
  document: "📄",
  anomaly: "⚠",
};

function relTime(iso: string | null): string {
  if (!iso) return "";
  const diff = Date.now() - new Date(iso).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "только что";
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч`;
  return new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

export default function InboxPage() {
  const [items, setItems] = useState<FeedItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [kind, setKind] = useState<(typeof KIND_FILTERS)[number]["key"]>("all");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    const kinds = kind === "all" ? "email,document,anomaly" : kind;
    setLoading(true);
    apiFetch(`${API}/api/inbox?kinds=${kinds}&limit=80`)
      .then((r) => (r.ok ? r.json() : { items: [], counts: {} }))
      .then((data) => {
        setItems(data.items ?? []);
        setCounts(data.counts ?? {});
      })
      .catch(() => setItems([]))
      .finally(() => setLoading(false));
  }, [kind]);

  useEffect(load, [load]);

  // Keyboard navigation stays what it was on the desktop: j/k/Enter.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      )
        return;
      if (e.key === "j") setSelectedIndex((i) => Math.min(i + 1, items.length - 1));
      else if (e.key === "k") setSelectedIndex((i) => Math.max(i - 1, 0));
      else if (e.key === "Enter") {
        const sel = items[selectedIndex];
        if (sel) window.location.href = sel.url;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, selectedIndex]);

  // Live updates, coalesced: a busy shared mailbox otherwise reloads the feed
  // on every single event.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const ws = new WebSocket(`${getWebSocketBaseUrl()}/api/notifications/ws`);
    ws.onmessage = () => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(load, 800);
    };
    ws.onerror = () => {};
    return () => ws.close();
  }, [load]);

  return (
    <div className="p-4 md:p-6">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <h1 className="mr-2 text-xl font-bold md:text-2xl">Входящие</h1>
        {KIND_FILTERS.map((f) => {
          const n = f.key === "all"
            ? Object.values(counts).reduce((a, b) => a + b, 0)
            : counts[f.key] ?? 0;
          return (
            <button
              key={f.key}
              onClick={() => setKind(f.key)}
              className={`rounded-md px-3 py-1.5 text-sm transition-colors ${
                kind === f.key
                  ? "bg-blue-500 text-white"
                  : "border border-slate-600 bg-slate-700 text-slate-300 hover:bg-slate-600"
              }`}
            >
              {f.label}
              {n > 0 && <span className="ml-1.5 opacity-70">{n}</span>}
            </button>
          );
        })}
      </div>

      {loading && items.length === 0 ? (
        <p className="py-10 text-center text-sm text-slate-400">…</p>
      ) : items.length === 0 ? (
        <p className="py-10 text-center text-sm text-slate-400">
          Ничего не ждёт вашего внимания.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800 overflow-hidden rounded-lg border border-slate-700">
          {items.map((item, i) => (
            <li key={`${item.kind}:${item.id}`}>
              <a
                href={item.url}
                onMouseEnter={() => setSelectedIndex(i)}
                className={`flex items-start gap-3 px-3 py-3 transition-colors md:px-4 ${
                  i === selectedIndex ? "bg-slate-800" : "hover:bg-slate-800/60"
                }`}
              >
                <span
                  className={`mt-0.5 text-base ${
                    item.kind === "anomaly" && item.severity === "critical"
                      ? "text-red-400"
                      : "text-slate-400"
                  }`}
                >
                  {KIND_ICON[item.kind]}
                </span>
                <span className="min-w-0 flex-1">
                  <span
                    className={`block truncate text-sm ${
                      item.unread ? "font-semibold text-slate-100" : "text-slate-200"
                    }`}
                  >
                    {item.title}
                  </span>
                  {item.subtitle && (
                    <span className="mt-0.5 block truncate text-xs text-slate-500">
                      {item.subtitle}
                    </span>
                  )}
                  {item.badge && (
                    <span className="mt-1 inline-block rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-slate-300">
                      {item.badge}
                    </span>
                  )}
                </span>
                <span className="shrink-0 text-[11px] text-slate-500">
                  {relTime(item.at)}
                </span>
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
