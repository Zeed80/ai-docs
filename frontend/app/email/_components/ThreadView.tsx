"use client";

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import type { EmailMessage, EmailThread } from "./types";

function MessageCard({
  msg,
  defaultOpen,
}: {
  msg: EmailMessage;
  defaultOpen: boolean;
}) {
  const t = useTranslations("email");
  const [open, setOpen] = useState(defaultOpen);
  const [status, setStatus] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const srcdoc = useMemo(() => {
    const html = msg.body_html_sanitized || msg.body_html;
    if (!html) return null;
    return `<!doctype html><meta charset="utf-8"><base target="_blank"><style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;color:#e2e8f0;background:transparent;margin:0}a{color:#60a5fa}img{max-width:100%}</style>${html}`;
  }, [msg.body_html_sanitized, msg.body_html]);

  async function process(filename: string, target: "document" | "drawing") {
    setBusy(filename);
    try {
      await emailApi.processAttachment(msg.id, filename, target);
      setStatus((s) => ({ ...s, [filename]: "✓" }));
    } catch (e) {
      setStatus((s) => ({ ...s, [filename]: e instanceof Error ? e.message : "err" }));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div
      className={`rounded-lg border ${msg.is_inbound ? "border-slate-700 bg-slate-800" : "border-blue-800 bg-blue-900/20"}`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-left"
      >
        <div className="flex min-w-0 items-center gap-2.5">
          <div
            className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
              msg.is_inbound ? "bg-slate-700 text-slate-300" : "bg-blue-600 text-white"
            }`}
          >
            {(msg.from_address[0] ?? "?").toUpperCase()}
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm text-slate-100">{msg.from_address}</p>
            {!open && (
              <p className="truncate text-xs text-slate-500">
                {msg.snippet || (msg.body_text ?? "").slice(0, 90)}
              </p>
            )}
          </div>
        </div>
        <span className="shrink-0 text-[11px] text-slate-500">
          {new Date(msg.sent_at || msg.received_at || "").toLocaleString("ru-RU", {
            day: "numeric",
            month: "short",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      </button>

      {open && (
        <div className="border-t border-slate-700 px-4 pb-4">
          {msg.to_addresses && (
            <p className="mt-2 text-xs text-slate-500">Кому: {msg.to_addresses.join(", ")}</p>
          )}
          {srcdoc ? (
            <iframe
              sandbox=""
              srcDoc={srcdoc}
              className="mt-3 w-full rounded bg-slate-900/40"
              style={{ height: 360, border: 0 }}
            />
          ) : (
            <div className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-slate-200">
              {msg.body_text || <span className="italic text-slate-500">Пустое письмо</span>}
            </div>
          )}

          {msg.attachments.length > 0 && (
            <div className="mt-3 space-y-1.5">
              {msg.attachments.map((a) => (
                <div
                  key={a.id}
                  className="flex flex-wrap items-center gap-2 rounded border border-slate-700 bg-slate-800/60 px-3 py-1.5"
                >
                  <a
                    href={emailApi.attachmentUrl(msg.id, a.filename)}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate text-xs font-mono text-blue-300 hover:underline"
                  >
                    {a.filename}
                  </a>
                  <button
                    onClick={() => process(a.filename, "document")}
                    disabled={busy === a.filename}
                    className="ml-auto rounded border border-slate-600 px-2 py-0.5 text-xs hover:bg-slate-700 disabled:opacity-50"
                  >
                    {t("attachmentTo.document")}
                  </button>
                  <button
                    onClick={() => process(a.filename, "drawing")}
                    disabled={busy === a.filename}
                    className="rounded border border-slate-600 px-2 py-0.5 text-xs hover:bg-slate-700 disabled:opacity-50"
                  >
                    {t("attachmentTo.drawing")}
                  </button>
                  {status[a.filename] && (
                    <span className="w-full text-[11px] text-slate-400">{status[a.filename]}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ThreadView({
  threadId,
  onReply,
  onForward,
  onArchive,
  onTrash,
  onClose,
}: {
  threadId: string;
  onReply: (m: EmailMessage, all?: boolean) => void;
  onForward: (m: EmailMessage) => void;
  onArchive: () => void;
  onTrash: () => void;
  onClose: () => void;
}) {
  const t = useTranslations("email");
  const [thread, setThread] = useState<EmailThread | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setThread(null);
    setError(null);
    emailApi
      .thread(threadId)
      .then((th) => {
        setThread(th);
        if (!th.is_read) emailApi.bulkAction([th.id], "read").catch(() => {});
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [threadId]);

  if (error) return <div className="p-6 text-sm text-red-400">{error}</div>;
  if (!thread) return <div className="p-6 text-sm text-slate-400">…</div>;

  const last = thread.messages[thread.messages.length - 1];

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-slate-700 px-4 py-2.5">
        <button onClick={onClose} className="text-slate-400 hover:text-slate-200 md:hidden">
          ←
        </button>
        <h2 className="flex-1 truncate text-sm font-semibold text-slate-100">
          {thread.subject || "(без темы)"}
        </h2>
        <button
          onClick={() => last && onReply(last)}
          className="rounded px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
        >
          {t("actions.reply")}
        </button>
        <button
          onClick={() => last && onReply(last, true)}
          className="rounded px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
        >
          {t("actions.replyAll")}
        </button>
        <button
          onClick={() => last && onForward(last)}
          className="rounded px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
        >
          {t("actions.forward")}
        </button>
        <button
          onClick={onArchive}
          className="rounded px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
        >
          {t("actions.archive")}
        </button>
        <button
          onClick={onTrash}
          className="rounded px-2 py-1 text-xs text-slate-400 hover:bg-red-900/40 hover:text-red-300"
        >
          {t("actions.trash")}
        </button>
      </div>
      <div className="flex-1 space-y-3 overflow-auto p-4">
        {thread.messages.map((m, i) => (
          <MessageCard key={m.id} msg={m} defaultOpen={i === thread.messages.length - 1} />
        ))}
      </div>
    </div>
  );
}
