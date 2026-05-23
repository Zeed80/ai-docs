"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

interface Handover {
  id: string;
  entity_type: string;
  entity_id: string;
  from_user: string;
  to_user: string;
  comment: string | null;
  status: string;
  created_at: string;
}

const ENTITY_LINKS: Record<string, string> = {
  document: "/documents",
  invoice: "/invoices",
  approval: "/approvals",
};

const STATUS_STYLES: Record<string, string> = {
  pending: "bg-amber-900/40 text-amber-300",
  accepted: "bg-green-900/40 text-green-300",
  forwarded: "bg-blue-900/40 text-blue-300",
  returned: "bg-slate-700 text-slate-400",
};

const STATUS_LABELS: Record<string, string> = {
  pending: "Ожидает",
  accepted: "Принято",
  forwarded: "Перенаправлено",
  returned: "Возвращено",
};

export default function HandoversPage() {
  const router = useRouter();
  const [tab, setTab] = useState<"inbox" | "outbox">("inbox");
  const [items, setItems] = useState<Handover[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionId, setActionId] = useState<string | null>(null);
  const [forwardTo, setForwardTo] = useState("");
  const [forwardComment, setForwardComment] = useState("");
  const [showForward, setShowForward] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    try {
      const res = await mutFetch(`${API}/api/handovers/${tab}`, {
        credentials: "include",
      });
      if (res.ok) setItems(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, [tab]);

  async function accept(id: string) {
    setActionId(id);
    try {
      await mutFetch(`${API}/api/handovers/${id}/accept`, {
        method: "POST",
        credentials: "include",
      });
      await load();
    } finally {
      setActionId(null);
    }
  }

  async function returnIt(id: string) {
    setActionId(id);
    try {
      await mutFetch(`${API}/api/handovers/${id}/return`, {
        method: "POST",
        credentials: "include",
      });
      await load();
    } finally {
      setActionId(null);
    }
  }

  async function forward(id: string) {
    if (!forwardTo.trim()) return;
    setActionId(id);
    try {
      await mutFetch(`${API}/api/handovers/${id}/forward`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          entity_type: items.find((h) => h.id === id)?.entity_type,
          entity_id: items.find((h) => h.id === id)?.entity_id,
          to_user: forwardTo.trim(),
          comment: forwardComment || null,
        }),
      });
      setShowForward(null);
      setForwardTo("");
      setForwardComment("");
      await load();
    } finally {
      setActionId(null);
    }
  }

  return (
    <div className="p-6 max-w-3xl mx-auto">
      <div className="flex items-center justify-between mb-5">
        <div>
          <h1 className="text-xl font-bold text-slate-100">Передачи</h1>
          <p className="text-xs text-slate-500 mt-0.5">
            Документы, переданные вам или отправленные другим
          </p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 mb-5">
        {(["inbox", "outbox"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-1.5 text-sm rounded transition-colors ${
              tab === t
                ? "bg-slate-600 text-slate-100"
                : "bg-slate-800 text-slate-400 hover:text-slate-200"
            }`}
          >
            {t === "inbox" ? "Входящие" : "Исходящие"}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="py-10 text-center text-slate-500 text-sm">
          Загрузка...
        </div>
      ) : items.length === 0 ? (
        <div className="py-16 text-center">
          <div className="text-4xl text-slate-700 mb-3">↗</div>
          <p className="text-slate-400 text-sm">
            {tab === "inbox"
              ? "Нет входящих передач."
              : "Вы ещё ничего не передавали."}
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((h) => {
            const entityBase =
              ENTITY_LINKS[h.entity_type] ?? `/${h.entity_type}s`;
            const entityHref = `${entityBase}/${h.entity_id}`;

            return (
              <div
                key={h.id}
                className="bg-slate-800 border border-slate-700 rounded-lg p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <button
                        onClick={() => router.push(entityHref)}
                        className="text-sm font-medium text-blue-400 hover:text-blue-300 hover:underline"
                      >
                        {h.entity_type} · {h.entity_id.slice(0, 8)}…
                      </button>
                      <span
                        className={`text-[10px] px-2 py-0.5 rounded-full ${STATUS_STYLES[h.status] ?? "bg-slate-700 text-slate-400"}`}
                      >
                        {STATUS_LABELS[h.status] ?? h.status}
                      </span>
                    </div>

                    <div className="mt-1 text-xs text-slate-400">
                      {tab === "inbox" ? (
                        <>
                          От:{" "}
                          <span className="font-mono">
                            {h.from_user.slice(0, 24)}
                          </span>
                        </>
                      ) : (
                        <>
                          Кому:{" "}
                          <span className="font-mono">
                            {h.to_user.slice(0, 24)}
                          </span>
                        </>
                      )}
                      <span className="mx-2">·</span>
                      {new Date(h.created_at).toLocaleString("ru-RU")}
                    </div>

                    {h.comment && (
                      <p className="mt-1.5 text-sm text-slate-300 italic">
                        «{h.comment}»
                      </p>
                    )}
                  </div>

                  {/* Actions for inbox pending */}
                  {tab === "inbox" && h.status === "pending" && (
                    <div className="flex gap-1.5 shrink-0">
                      <button
                        onClick={() => accept(h.id)}
                        disabled={actionId === h.id}
                        className="px-3 py-1 text-xs bg-green-700 hover:bg-green-600 text-white rounded disabled:opacity-50"
                      >
                        Принять
                      </button>
                      <button
                        onClick={() =>
                          setShowForward(showForward === h.id ? null : h.id)
                        }
                        className="px-3 py-1 text-xs bg-blue-700 hover:bg-blue-600 text-white rounded"
                      >
                        Передать
                      </button>
                      <button
                        onClick={() => returnIt(h.id)}
                        disabled={actionId === h.id}
                        className="px-3 py-1 text-xs bg-slate-600 hover:bg-slate-500 text-white rounded disabled:opacity-50"
                      >
                        Вернуть
                      </button>
                    </div>
                  )}
                </div>

                {/* Forward form */}
                {showForward === h.id && (
                  <div className="mt-3 pt-3 border-t border-slate-700 space-y-2">
                    <input
                      autoFocus
                      type="text"
                      value={forwardTo}
                      onChange={(e) => setForwardTo(e.target.value)}
                      placeholder="Логин или email получателя *"
                      className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
                    />
                    <input
                      type="text"
                      value={forwardComment}
                      onChange={(e) => setForwardComment(e.target.value)}
                      placeholder="Комментарий (необязательно)"
                      className="w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded outline-none focus:border-blue-400"
                    />
                    <div className="flex gap-2 justify-end">
                      <button
                        onClick={() => setShowForward(null)}
                        className="px-3 py-1 text-xs text-slate-400"
                      >
                        Отмена
                      </button>
                      <button
                        onClick={() => forward(h.id)}
                        disabled={!forwardTo.trim() || actionId === h.id}
                        className="px-4 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
                      >
                        {actionId === h.id ? "..." : "Передать"}
                      </button>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
