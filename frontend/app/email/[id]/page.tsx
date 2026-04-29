"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

interface EmailMessage {
  id: string;
  from_address: string;
  to_addresses: string[] | null;
  subject: string | null;
  body_text: string | null;
  sent_at: string | null;
  received_at: string | null;
  is_inbound: boolean;
  has_attachments: boolean;
  attachment_count: number;
}

interface EmailThread {
  id: string;
  subject: string;
  mailbox: string;
  message_count: number;
  last_message_at: string | null;
  messages: EmailMessage[];
}

interface DraftForm {
  to_address: string;
  subject: string;
  body: string;
}

export default function EmailThreadPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [thread, setThread] = useState<EmailThread | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [replying, setReplying] = useState(false);
  const [draft, setDraft] = useState<DraftForm>({
    to_address: "",
    subject: "",
    body: "",
  });
  const [sending, setSending] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    fetch(`${API}/api/email/threads/${id}`)
      .then((r) => {
        if (r.status === 404) {
          setNotFound(true);
          return null;
        }
        return r.json();
      })
      .then((data) => {
        if (data) {
          setThread(data);
          // Pre-fill reply form
          const lastInbound = [...(data.messages ?? [])]
            .reverse()
            .find((m: EmailMessage) => m.is_inbound);
          if (lastInbound) {
            setDraft({
              to_address: lastInbound.from_address,
              subject: `Re: ${data.subject}`,
              body: "",
            });
          }
        }
      })
      .catch(() => setNotFound(true))
      .finally(() => setLoading(false));
  }, [id]);

  async function saveDraft() {
    setSending(true);
    try {
      const res = await fetch(`${API}/api/email/drafts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          to_addresses: [draft.to_address],
          subject: draft.subject,
          body_text: draft.body,
          thread_id: id,
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      setToast("Черновик сохранён");
      setReplying(false);
      setTimeout(() => setToast(null), 3000);
    } catch (e) {
      setToast(`Ошибка: ${String(e).slice(0, 60)}`);
    } finally {
      setSending(false);
    }
  }

  async function runRiskCheck() {
    setSending(true);
    try {
      const res = await fetch(`${API}/api/email/risk-check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: draft.body, subject: draft.subject }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const flags: string[] = data.risk_flags ?? [];
      setToast(
        flags.length === 0 ? "Риск-проверка: OK" : `Риск: ${flags.join(", ")}`,
      );
      setTimeout(() => setToast(null), 5000);
    } catch {
      setToast("Риск-проверка недоступна");
      setTimeout(() => setToast(null), 3000);
    } finally {
      setSending(false);
    }
  }

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-slate-400 text-sm">
        Загрузка...
      </div>
    );
  }

  if (notFound || !thread) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center text-slate-400">
          <p className="text-sm font-medium">Тред не найден</p>
          <button
            onClick={() => router.push("/email")}
            className="mt-3 text-xs text-blue-500 hover:underline"
          >
            ← Вернуться к письмам
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-6 py-4 border-b border-slate-200 shrink-0">
        <div className="flex items-start gap-3">
          <button
            onClick={() => router.push("/email")}
            className="text-slate-400 hover:text-slate-600 mt-0.5"
            title="Назад"
          >
            <svg
              className="w-5 h-5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M10 19l-7-7m0 0l7-7m-7 7h18"
              />
            </svg>
          </button>
          <div className="flex-1 min-w-0">
            <h1 className="text-lg font-semibold truncate">{thread.subject}</h1>
            <p className="text-xs text-slate-400 mt-0.5">
              {thread.mailbox} · {thread.message_count} сообщ.
              {thread.last_message_at &&
                ` · ${new Date(thread.last_message_at).toLocaleString("ru-RU", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`}
            </p>
          </div>
          <button
            onClick={() => setReplying((r) => !r)}
            className="px-3 py-1.5 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 shrink-0"
          >
            Ответить
          </button>
        </div>
      </div>

      {/* Toast */}
      {toast && (
        <div className="mx-6 mt-3 px-4 py-2 rounded-lg text-sm bg-blue-50 text-blue-700 border border-blue-200 shrink-0">
          {toast}
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {thread.messages.length === 0 ? (
          <p className="text-sm text-slate-400 text-center py-8">
            Сообщений нет
          </p>
        ) : (
          thread.messages.map((msg, i) => (
            <MessageCard key={msg.id} msg={msg} index={i} />
          ))
        )}
      </div>

      {/* Reply form */}
      {replying && (
        <div className="border-t border-slate-200 p-4 bg-slate-50 shrink-0">
          <h3 className="text-sm font-medium mb-3 text-slate-700">Ответ</h3>
          <div className="space-y-2">
            <input
              value={draft.to_address}
              onChange={(e) =>
                setDraft((d) => ({ ...d, to_address: e.target.value }))
              }
              placeholder="Кому"
              className="w-full text-sm border border-slate-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <input
              value={draft.subject}
              onChange={(e) =>
                setDraft((d) => ({ ...d, subject: e.target.value }))
              }
              placeholder="Тема"
              className="w-full text-sm border border-slate-200 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <textarea
              value={draft.body}
              onChange={(e) =>
                setDraft((d) => ({ ...d, body: e.target.value }))
              }
              placeholder="Текст письма..."
              rows={4}
              className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
            />
          </div>
          <div className="flex gap-2 mt-3">
            <button
              onClick={saveDraft}
              disabled={sending || !draft.to_address || !draft.body}
              className="px-4 py-1.5 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
            >
              Сохранить черновик
            </button>
            <button
              onClick={runRiskCheck}
              disabled={sending || !draft.body}
              className="px-4 py-1.5 bg-slate-200 text-slate-700 text-sm rounded-lg hover:bg-slate-300 disabled:opacity-50"
            >
              Риск-проверка
            </button>
            <button
              onClick={() => setReplying(false)}
              className="px-3 py-1.5 text-slate-400 text-sm hover:text-slate-600"
            >
              Отмена
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function MessageCard({ msg, index }: { msg: EmailMessage; index: number }) {
  const [expanded, setExpanded] = useState(index === 0 || index === -1);
  const date = msg.sent_at ?? msg.received_at;

  return (
    <div
      className={`rounded-lg border ${msg.is_inbound ? "border-slate-200 bg-white" : "border-blue-100 bg-blue-50"}`}
    >
      {/* Message header — always visible */}
      <button
        className="w-full flex items-center justify-between px-4 py-3 text-left"
        onClick={() => setExpanded((e) => !e)}
      >
        <div className="flex items-center gap-3 min-w-0">
          <div
            className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold shrink-0 ${
              msg.is_inbound
                ? "bg-slate-200 text-slate-600"
                : "bg-blue-500 text-white"
            }`}
          >
            {(msg.from_address[0] ?? "?").toUpperCase()}
          </div>
          <div className="min-w-0">
            <p className="text-sm font-medium truncate">{msg.from_address}</p>
            {!expanded && msg.body_text && (
              <p className="text-xs text-slate-400 truncate">
                {msg.body_text.slice(0, 80)}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0 ml-3">
          {msg.has_attachments && (
            <span className="text-xs text-slate-400 flex items-center gap-1">
              <svg
                className="w-3.5 h-3.5"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"
                />
              </svg>
              {msg.attachment_count}
            </span>
          )}
          {date && (
            <span className="text-xs text-slate-400">
              {new Date(date).toLocaleString("ru-RU", {
                day: "numeric",
                month: "short",
                hour: "2-digit",
                minute: "2-digit",
              })}
            </span>
          )}
          <svg
            className={`w-4 h-4 text-slate-400 transition-transform ${expanded ? "rotate-180" : ""}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M19 9l-7 7-7-7"
            />
          </svg>
        </div>
      </button>

      {/* Message body */}
      {expanded && (
        <div className="px-4 pb-4 border-t border-slate-100">
          {msg.to_addresses && msg.to_addresses.length > 0 && (
            <p className="text-xs text-slate-400 mt-2">
              Кому: {msg.to_addresses.join(", ")}
            </p>
          )}
          <div className="mt-3 text-sm text-slate-700 whitespace-pre-wrap leading-relaxed">
            {msg.body_text ?? (
              <span className="text-slate-400 italic">
                Тело письма отсутствует
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
