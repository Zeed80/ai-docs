"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import { RichTextEditor } from "./RichTextEditor";
import type { ComposeMode, EmailMessage, MailboxChip } from "./types";

function htmlToText(html: string): string {
  const el = document.createElement("div");
  el.innerHTML = html;
  return (el.textContent ?? "").trim();
}

function quote(msg: EmailMessage): string {
  const when = msg.sent_at || msg.received_at || "";
  const who = msg.from_address.replace(/\s*<[^>]+>\s*/, "").trim() || msg.from_address;
  const head = `— ${new Date(when).toLocaleString("ru-RU")}, ${who}:`;
  return `<br/><br/><blockquote style="border-left:2px solid #64748b;padding-left:8px;color:#94a3b8">${head}<br/>${
    msg.body_html || (msg.body_text || "").replace(/\n/g, "<br/>")
  }</blockquote>`;
}

export function Composer({
  mode,
  mailboxes,
  defaultMailbox,
  onClose,
  onSent,
}: {
  mode: ComposeMode;
  mailboxes: MailboxChip[];
  defaultMailbox: string;
  onClose: () => void;
  onSent: () => void;
}) {
  const t = useTranslations("email");
  const tc = useTranslations("email.composer");

  const initial = useMemo(() => {
    if (mode.kind === "reply") {
      const m = mode.message;
      const to = m.is_inbound ? [m.from_address] : m.to_addresses ?? [];
      const cc = mode.all ? (m.cc_addresses ?? []) : [];
      return {
        to,
        cc,
        bcc: [] as string[],
        subject: /^re:/i.test(m.subject ?? "") ? m.subject ?? "" : `Re: ${m.subject ?? ""}`,
        body: quote(m),
        mailbox: m.mailbox || defaultMailbox,
        inReplyTo: m.id as string | null,
      };
    }
    if (mode.kind === "forward") {
      const m = mode.message;
      return {
        to: [] as string[],
        cc: [] as string[],
        bcc: [] as string[],
        subject: /^fwd:/i.test(m.subject ?? "") ? m.subject ?? "" : `Fwd: ${m.subject ?? ""}`,
        body: quote(m),
        mailbox: m.mailbox || defaultMailbox,
        inReplyTo: null as string | null,
      };
    }
    if (mode.kind === "draft") {
      const d = mode.draft;
      return {
        to: d.to_addresses ?? [],
        cc: d.cc_addresses ?? [],
        bcc: d.bcc_addresses ?? [],
        subject: d.subject ?? "",
        body: d.body_html ?? "",
        mailbox: d.mailbox || defaultMailbox,
        inReplyTo: null as string | null,
      };
    }
    return {
      to: [] as string[],
      cc: [] as string[],
      bcc: [] as string[],
      subject: "",
      body: "",
      mailbox: defaultMailbox,
      inReplyTo: null as string | null,
    };
  }, [mode, defaultMailbox]);

  const [to, setTo] = useState(initial.to.join(", "));
  const [cc, setCc] = useState(initial.cc.join(", "));
  const [bcc, setBcc] = useState(initial.bcc.join(", "));
  const [showCc, setShowCc] = useState(initial.cc.length > 0 || initial.bcc.length > 0);
  const [subject, setSubject] = useState(initial.subject);
  const [body, setBody] = useState(initial.body);
  const [mailbox, setMailbox] = useState(initial.mailbox);
  const [attachments, setAttachments] = useState<{ id: string; filename: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiElapsed, setAiElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [draftId, setDraftId] = useState<string | null>(
    mode.kind === "draft" ? mode.draft.id : null,
  );
  const [suggest, setSuggest] = useState<NonNullable<
    Awaited<ReturnType<typeof emailApi.pollComposeAssist>>["result"]
  > | null>(null);
  const [aiInstruction, setAiInstruction] = useState("");
  const [contacts, setContacts] = useState<string[]>([]);

  const bareEmail = (s: string) => {
    const m = s.match(/<([^>]+)>/);
    return (m ? m[1] : s).trim();
  };
  const parseAddrs = (s: string) =>
    s
      .split(/[,;]/)
      .map((x) => bareEmail(x.trim()))
      .filter((x) => x.includes("@"));

  // Contact autocomplete for the To field.
  const toRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const last = to.split(/[,;]/).pop()?.trim() ?? "";
    if (last.length < 2) {
      setContacts([]);
      return;
    }
    const h = setTimeout(() => {
      emailApi
        .contacts(last)
        .then((cs) => setContacts(cs.map((c) => c.email)))
        .catch(() => setContacts([]));
    }, 250);
    return () => clearTimeout(h);
  }, [to]);

  async function persistDraft(): Promise<string> {
    const payload = {
      to_addresses: parseAddrs(to),
      cc_addresses: parseAddrs(cc),
      bcc_addresses: parseAddrs(bcc),
      subject,
      body_html: body,
      body_text: htmlToText(body),
      mailbox,
      attachment_ids: attachments.map((a) => a.id),
      in_reply_to_message_id: initial.inReplyTo,
    };
    if (draftId) {
      await emailApi.updateDraft(draftId, payload);
      return draftId;
    }
    const d = await emailApi.createDraft(payload);
    setDraftId(d.id);
    return d.id;
  }

  async function handleSaveDraft() {
    setBusy(true);
    setError(null);
    try {
      await persistDraft();
      onSent();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleSend() {
    if (!parseAddrs(to).length) {
      setError("Укажите получателя");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await emailApi.send({
        mailbox,
        to_addresses: parseAddrs(to),
        cc_addresses: parseAddrs(cc),
        bcc_addresses: parseAddrs(bcc),
        subject,
        body_html: body,
        body_text: htmlToText(body),
        attachment_ids: attachments.map((a) => a.id),
        in_reply_to_message_id: initial.inReplyTo,
        draft_id: draftId,
      });
      onSent();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleUpload(files: FileList | null) {
    if (!files) return;
    for (const f of Array.from(files)) {
      try {
        const a = await emailApi.uploadAttachment(f);
        setAttachments((prev) => [...prev, { id: a.id, filename: a.filename }]);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }

  async function handleAiHelp(instruction: string) {
    setAiBusy(true);
    setAiElapsed(0);
    setError(null);
    const started = Date.now();
    const tick = setInterval(
      () => setAiElapsed(Math.round((Date.now() - started) / 1000)),
      1000,
    );
    try {
      const { task_id } = await emailApi.startComposeAssist({
        subject,
        body: htmlToText(body),
        instruction:
          instruction || aiInstruction || "Улучши формулировки, сохрани смысл",
        thread_id: mode.kind === "reply" || mode.kind === "forward" ? mode.message.thread_id : undefined,
        mailbox,
      });
      // Poll up to ~5 min.
      for (let i = 0; i < 100; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const p = await emailApi.pollComposeAssist(task_id);
        if (p.status === "done" && p.result) {
          setSuggest(p.result);
          return;
        }
        if (p.status === "error") {
          setError(p.error || "Агент не смог доработать письмо");
          return;
        }
      }
      setError("Агент не успел ответить, попробуйте ещё раз");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      clearInterval(tick);
      setAiBusy(false);
    }
  }

  const input =
    "w-full px-3 py-1.5 text-sm bg-slate-700 border border-slate-600 text-slate-100 placeholder-slate-500 rounded focus:outline-none focus:ring-1 focus:ring-blue-500";

  return (
    <div className="flex flex-col h-full bg-slate-900">
      <div className="flex items-center justify-between border-b border-slate-700 px-4 py-2">
        <h3 className="text-sm font-semibold text-slate-100">
          {mode.kind === "reply"
            ? tc("subject") + ": " + subject
            : mode.kind === "forward"
              ? "Fwd"
              : t("compose")}
        </h3>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-200 text-lg leading-none">
          ×
        </button>
      </div>

      <div className="flex-1 overflow-auto p-4 space-y-2">
        {mailboxes.length > 1 && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-slate-400 w-10">{tc("from")}</span>
            <select
              value={mailbox}
              onChange={(e) => setMailbox(e.target.value)}
              className="bg-slate-700 border border-slate-600 text-slate-100 rounded px-2 py-1"
            >
              {mailboxes.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.display_name || m.name}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="flex items-center gap-2">
          <span className="text-slate-400 w-10 text-xs">{tc("to")}</span>
          <input
            ref={toRef}
            list="email-contacts"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="name@example.com"
            className={input}
          />
          <datalist id="email-contacts">
            {contacts.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
          {!showCc && (
            <button
              onClick={() => setShowCc(true)}
              className="text-xs text-slate-400 hover:text-slate-200 shrink-0"
            >
              Cc/Bcc
            </button>
          )}
        </div>
        {showCc && (
          <>
            <div className="flex items-center gap-2">
              <span className="text-slate-400 w-10 text-xs">{tc("cc")}</span>
              <input value={cc} onChange={(e) => setCc(e.target.value)} className={input} />
            </div>
            <div className="flex items-center gap-2">
              <span className="text-slate-400 w-10 text-xs">{tc("bcc")}</span>
              <input value={bcc} onChange={(e) => setBcc(e.target.value)} className={input} />
            </div>
          </>
        )}
        <div className="flex items-center gap-2">
          <span className="text-slate-400 w-10 text-xs">{tc("subject")}</span>
          <input value={subject} onChange={(e) => setSubject(e.target.value)} className={input} />
        </div>

        <RichTextEditor value={body} onChange={setBody} placeholder={tc("body")} />

        {attachments.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {attachments.map((a) => (
              <span
                key={a.id}
                className="flex items-center gap-1 text-xs bg-slate-800 border border-slate-700 rounded px-2 py-1 text-slate-300"
              >
                📎 {a.filename}
                <button
                  onClick={() => setAttachments((p) => p.filter((x) => x.id !== a.id))}
                  className="text-slate-500 hover:text-red-400"
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        )}

        {/* AI help */}
        <div className="border border-slate-700 rounded-lg p-2 space-y-2 bg-slate-800/50">
          <div className="flex flex-wrap gap-1.5">
            {(tc.raw("aiPresets") as string[]).map((p) => (
              <button
                key={p}
                onClick={() => handleAiHelp(p)}
                disabled={aiBusy}
                className="text-xs px-2 py-0.5 rounded-full border border-slate-600 text-slate-300 hover:bg-slate-700 disabled:opacity-50"
              >
                {p}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <input
              value={aiInstruction}
              onChange={(e) => setAiInstruction(e.target.value)}
              placeholder={tc("aiInstruction")}
              className={input}
            />
            <button
              onClick={() => handleAiHelp("")}
              disabled={aiBusy}
              className="text-xs px-3 py-1.5 rounded bg-violet-600 hover:bg-violet-500 text-white disabled:opacity-50 shrink-0"
            >
              {aiBusy ? `Агент работает… ${aiElapsed} с` : t("actions.aiHelp")}
            </button>
          </div>
        </div>

        {error && <p className="text-xs text-red-400">{error}</p>}
      </div>

      <div className="flex items-center gap-2 border-t border-slate-700 px-4 py-2">
        <button
          onClick={handleSend}
          disabled={busy}
          className="px-4 py-1.5 text-sm bg-blue-600 hover:bg-blue-500 text-white rounded disabled:opacity-50"
        >
          {busy ? tc("sending") : t("actions.send")}
        </button>
        <button
          onClick={handleSaveDraft}
          disabled={busy}
          className="px-3 py-1.5 text-sm border border-slate-600 text-slate-300 rounded hover:bg-slate-700 disabled:opacity-50"
        >
          {t("actions.saveDraft")}
        </button>
        <label className="px-3 py-1.5 text-sm border border-slate-600 text-slate-300 rounded hover:bg-slate-700 cursor-pointer">
          📎 {tc("attach")}
          <input type="file" multiple hidden onChange={(e) => handleUpload(e.target.files)} />
        </label>
      </div>

      {suggest && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
          <div className="bg-slate-800 border border-slate-700 rounded-xl max-w-3xl w-full max-h-[80vh] overflow-auto p-4">
            <h4 className="text-sm font-semibold text-slate-100 mb-3">{tc("aiPreviewTitle")}</h4>
            <div className="grid grid-cols-2 gap-3 text-sm">
              <div>
                <p className="text-xs text-slate-500 mb-1">Сейчас</p>
                <div className="border border-slate-700 rounded p-2 text-slate-300 whitespace-pre-wrap text-xs max-h-[40vh] overflow-auto">
                  {htmlToText(body)}
                </div>
              </div>
              <div>
                <p className="text-xs text-slate-500 mb-1">Предложение</p>
                <div className="border border-violet-700 rounded p-2 text-slate-100 whitespace-pre-wrap text-xs max-h-[40vh] overflow-auto">
                  {suggest.body_text}
                </div>
              </div>
            </div>
            {suggest.notes?.length > 0 && (
              <ul className="mt-2 text-xs text-slate-400 list-disc pl-4">
                {suggest.notes.map((n: string, i: number) => (
                  <li key={i}>{n}</li>
                ))}
              </ul>
            )}
            <div className="flex gap-2 mt-4">
              <button
                onClick={() => {
                  setBody(suggest.body_html || suggest.body_text.replace(/\n/g, "<br/>"));
                  if (suggest.subject) setSubject(suggest.subject);
                  setSuggest(null);
                }}
                className="px-3 py-1.5 text-sm bg-violet-600 hover:bg-violet-500 text-white rounded"
              >
                {tc("aiAccept")}
              </button>
              <button
                onClick={() => setSuggest(null)}
                className="px-3 py-1.5 text-sm border border-slate-600 text-slate-300 rounded hover:bg-slate-700"
              >
                {tc("aiReject")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
