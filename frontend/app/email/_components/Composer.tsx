"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import { RichTextEditor } from "./RichTextEditor";
// Ф7.3 — the phone can already scan and dictate; both were wired for documents
// and unavailable exactly where a person answers a supplier from the road.
import { dictate, isNative, scanDocument, speechAvailable } from "@/lib/native-bridge";
import { RecipientInput } from "./RecipientInput";
import type { ComposeMode, EmailMessage, MailboxChip } from "./types";

// Seconds a sent message can still be recalled. Long enough to notice the
// mistake, short enough that nobody closes the tab believing it has gone.
const UNDO_SECONDS = 10;

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
    // Sanitized HTML, never the raw body: the quote goes straight into the
    // editor, i.e. into our own DOM. The stored sanitized copy is what the
    // reader already renders.
    msg.body_html_sanitized ||
    msg.body_html ||
    (msg.body_text || "").replace(/\n/g, "<br/>")
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
      // Reply-To wins over From (Ф1.2): a supplier mailing from no-reply@ with
      // Reply-To: sales@ must get the answer at sales@.
      const to = m.is_inbound
        ? [m.reply_to || m.from_address]
        : m.to_addresses ?? [];
      // Ф5.2 — "ответить всем" used to mean "sender + original Cc", quietly
      // dropping everyone in the original To. Correct: everybody the message
      // reached, minus ourselves.
      const ours = new Set(
        mailboxes.map((mb) => mb.name.toLowerCase()).filter((x) => x.includes("@")),
      );
      const cc = mode.all
        ? [...(m.to_addresses ?? []), ...(m.cc_addresses ?? [])]
            .map((a) => bareEmail(a).toLowerCase())
            .filter(
              (a) =>
                a &&
                !ours.has(a) &&
                !to.map((t) => bareEmail(t).toLowerCase()).includes(a),
            )
            .filter((a, i, arr) => arr.indexOf(a) === i)
        : [];
      return {
        to,
        cc,
        bcc: [] as string[],
        subject: /^re:/i.test(m.subject ?? "") ? m.subject ?? "" : `Re: ${m.subject ?? ""}`,
        body: "",
        quote: quote(m),
        mailbox: m.mailbox || defaultMailbox,
        inReplyTo: m.id as string | null,
        forwardOf: null as string | null,
        forwardAttachments: [] as { id: string; filename: string }[],
      };
    }
    if (mode.kind === "forward") {
      const m = mode.message;
      return {
        to: [] as string[],
        cc: [] as string[],
        bcc: [] as string[],
        subject: /^fwd:/i.test(m.subject ?? "") ? m.subject ?? "" : `Fwd: ${m.subject ?? ""}`,
        body: "",
        quote: quote(m),
        mailbox: m.mailbox || defaultMailbox,
        inReplyTo: null as string | null,
        // Ф5.2 — forwarding used to drop the attachments entirely: the field
        // existed in the schema and the frontend never sent it, so "перешлю
        // счёт" arrived without the счёт.
        forwardOf: m.id as string | null,
        forwardAttachments: (m.attachments ?? []).map((a) => ({
          id: a.id,
          filename: a.filename,
        })),
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
        quote: "",
        mailbox: d.mailbox || defaultMailbox,
        inReplyTo: null as string | null,
        forwardOf: null as string | null,
        forwardAttachments: [] as { id: string; filename: string }[],
      };
    }
    return {
      to: (mode.kind === "new" && mode.to) || ([] as string[]),
      cc: [] as string[],
      bcc: [] as string[],
      subject: "",
      body: "",
      quote: "",
      mailbox: defaultMailbox,
      inReplyTo: null as string | null,
      forwardOf: null as string | null,
      forwardAttachments: [] as { id: string; filename: string }[],
    };
  }, [mode, defaultMailbox]);

  const bareEmail = (s: string) => {
    const m = s.match(/<([^>]+)>/);
    return (m ? m[1] : s).trim();
  };

  const [to, setTo] = useState<string[]>(initial.to.map((x) => bareEmail(x).toLowerCase()));
  const [cc, setCc] = useState<string[]>(initial.cc.map((x) => bareEmail(x).toLowerCase()));
  const [bcc, setBcc] = useState<string[]>(initial.bcc.map((x) => bareEmail(x).toLowerCase()));
  const [showCc, setShowCc] = useState(initial.cc.length > 0 || initial.bcc.length > 0);
  const [subject, setSubject] = useState(initial.subject);
  const [body, setBody] = useState(initial.body);
  // Ф5.2 — цитата хранится ОТДЕЛЬНО и не редактируется. Раньше она попадала в
  // тело: TipTap без расширений для таблиц выбрасывал таблицу делового письма,
  // и процитированная спецификация превращалась в столбик слов. Теперь
  // оригинал не проходит через редактор вовсе и склеивается при отправке.
  const [quoted] = useState(initial.quote);
  const [quoteOpen, setQuoteOpen] = useState(false);
  const composed = quoted ? `${body}${quoted}` : body;
  const [sigApplied, setSigApplied] = useState(false);
  const [mailbox, setMailbox] = useState(initial.mailbox);
  // Вложения, живущие ВНУТРИ тела письма: в списке вложений им не место,
  // но отправить их нужно вместе с письмом.
  const [inlineIds, setInlineIds] = useState<string[]>([]);
  const [attachments, setAttachments] = useState<{ id: string; filename: string }[]>(
    initial.forwardAttachments ?? [],
  );

  // Files staged by the mobile "Поделиться → Приложить к письму" flow: we have
  // ids but not names until the draft is saved, so show a neutral placeholder
  // rather than pretending to know the filename.
  useEffect(() => {
    if (mode.kind !== "new" || !mode.attachmentIds?.length) return;
    setAttachments((prev) => {
      const known = new Set(prev.map((a) => a.id));
      const extra = mode.attachmentIds!
        .filter((id) => !known.has(id))
        .map((id, i) => ({ id, filename: `Вложение ${i + 1}` }));
      return extra.length ? [...prev, ...extra] : prev;
    });
  }, [mode]);
  const [busy, setBusy] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiElapsed, setAiElapsed] = useState(0);
  const [aiStep, setAiStep] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [draftId, setDraftId] = useState<string | null>(
    mode.kind === "draft" ? mode.draft.id : null,
  );
  const [suggest, setSuggest] = useState<NonNullable<
    Awaited<ReturnType<typeof emailApi.pollComposeAssist>>["result"]
  > | null>(null);
  const [aiInstruction, setAiInstruction] = useState("");
  // Ф4: risks that stopped the send, and the recall window after it started.
  const [blocked, setBlocked] = useState<{ code: string; message: string }[]>([]);
  const [sentUndo, setSentUndo] = useState<{ draftId: string; until: number } | null>(null);
  const [undoLeft, setUndoLeft] = useState(0);

  useEffect(() => {
    if (!sentUndo) return;
    const tick = () => {
      const left = Math.ceil((sentUndo.until - Date.now()) / 1000);
      setUndoLeft(Math.max(0, left));
      if (left <= 0) {
        setSentUndo(null);
        onClose();
      }
    };
    tick();
    const id = setInterval(tick, 500);
    return () => clearInterval(id);
  }, [sentUndo, onClose]);

  // Prefill the applicable signature once, at the bottom of the draft.
  useEffect(() => {
    if (sigApplied || mode.kind === "draft") return;
    setSigApplied(true);
    emailApi.resolveSignature(mailbox).then((sig) => {
      if (sig?.body_html) {
        setBody((b) => `${b}<br/><br/>${sig.body_html}`);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mailbox]);


  async function persistDraft(): Promise<string> {
    const payload = {
      to_addresses: to,
      cc_addresses: cc,
      bcc_addresses: bcc,
      subject,
      body_html: composed,
      body_text: htmlToText(composed),
      mailbox,
      attachment_ids: [...attachments.map((a) => a.id), ...inlineIds],
      in_reply_to_message_id: initial.inReplyTo,
      forward_of_message_id: initial.forwardOf,
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

  async function handleSend(acknowledged: string[] = []) {
    if (!to.length) {
      setError(t("needRecipient"));
      return;
    }
    setBusy(true);
    setError(null);
    setBlocked([]);
    try {
      const res = await emailApi.send({
        mailbox,
        to_addresses: to,
        cc_addresses: cc,
        bcc_addresses: bcc,
        subject,
        body_html: composed,
        body_text: htmlToText(composed),
        attachment_ids: [...attachments.map((a) => a.id), ...inlineIds],
        in_reply_to_message_id: initial.inReplyTo,
        forward_of_message_id: initial.forwardOf,
        draft_id: draftId,
        acknowledged_risks: acknowledged,
        delay_seconds: UNDO_SECONDS,
      });
      if (res.status === "blocked") {
        // Ф4: the send was refused — show what and let the person decide,
        // instead of silently sending (the old behaviour) or silently failing.
        setBlocked(res.blocked_by);
        setDraftId(res.draft_id);
        return;
      }
      if (res.undo_seconds > 0) {
        onSent();
        setSentUndo({ draftId: res.draft_id, until: Date.now() + res.undo_seconds * 1000 });
        return;
      }
      onSent();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function handleUndo() {
    if (!sentUndo) return;
    try {
      await emailApi.cancelSend(sentUndo.draftId);
      setSentUndo(null);
      setError(t("sendCancelled"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // Ф5.2 — autosave. `update_draft` is documented as "Autosave / edit in
  // place" and the client never called it periodically, so closing the window
  // lost the letter with no warning at all.
  const dirtyRef = useRef(false);
  useEffect(() => {
    dirtyRef.current = true;
  }, [to, cc, bcc, subject, body, attachments, mailbox]);

  useEffect(() => {
    if (sentUndo) return;
    const id = setInterval(() => {
      if (!dirtyRef.current) return;
      if (!subject.trim() && !htmlToText(body).trim() && !to.length) return;
      dirtyRef.current = false;
      persistDraft().catch(() => {
        dirtyRef.current = true;
      });
    }, 15000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subject, body, to, cc, bcc, attachments, mailbox, sentUndo]);

  function requestClose() {
    if (dirtyRef.current && (subject.trim() || htmlToText(body).trim() || to.length)) {
      if (!window.confirm(t("unsavedConfirm"))) return;
    }
    onClose();
  }

  const [native, setNative] = useState(false);
  const [canDictate, setCanDictate] = useState(false);
  const [listening, setListening] = useState(false);

  useEffect(() => {
    if (!isNative()) return;
    setNative(true);
    speechAvailable().then(setCanDictate).catch(() => setCanDictate(false));
  }, []);

  async function handleScan() {
    try {
      const files = await scanDocument();
      if (files.length) await handleUpload(files as unknown as FileList);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDictate() {
    setListening(true);
    try {
      const text = await dictate("ru-RU");
      if (text) setBody((b) => `${b}${b ? "<br/>" : ""}${text}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setListening(false);
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

  // Ф6.9 — «Света, ответь»: черновик готовит агент, отправляет человек.
  // Гейт на email.send остаётся на месте — здесь только заполняется тело.
  const assistStarted = useRef(false);
  useEffect(() => {
    const ask = mode.kind === "reply" ? mode.assist : undefined;
    if (!ask || assistStarted.current) return;
    assistStarted.current = true;
    void handleAiHelp(ask);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode]);

  /**
   * Ф5.2 — вставленная в тело картинка грузится как вложение и показывается
   * по временному URL. В списке вложений она не появляется: отправитель имел
   * в виду картинку в письме, а не файл рядом с ним. При отправке
   * `email_sender` находит её по `data-attachment-id` и превращает в
   * inline-часть `cid:`.
   */
  const handleImagePaste = useCallback(
    async (file: File): Promise<{ id: string; url: string } | null> => {
      try {
        const a = await emailApi.uploadAttachment(file);
        setInlineIds((prev) => [...prev, a.id]);
        return { id: a.id, url: URL.createObjectURL(file) };
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return null;
      }
    },
    [],
  );

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
        if (p.progress?.length) setAiStep(p.progress[p.progress.length - 1]);
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

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        handleSend();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [to, cc, bcc, subject, body, attachments, mailbox]);

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
        <button
          onClick={requestClose}
          aria-label="Закрыть"
          className="text-lg leading-none text-slate-400 hover:text-slate-200"
        >
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
        <div className="flex items-start gap-2">
          <RecipientInput label={tc("to")} value={to} onChange={setTo} autoFocus />
          {!showCc && (
            <button
              onClick={() => setShowCc(true)}
              className="mt-1 shrink-0 text-xs text-slate-400 hover:text-slate-200"
            >
              Cc/Bcc
            </button>
          )}
        </div>
        {showCc && (
          <>
            <RecipientInput label={tc("cc")} value={cc} onChange={setCc} />
            <RecipientInput label={tc("bcc")} value={bcc} onChange={setBcc} />
          </>
        )}
        <div className="flex items-center gap-2">
          <span className="text-slate-400 w-10 text-xs">{tc("subject")}</span>
          <input value={subject} onChange={(e) => setSubject(e.target.value)} className={input} />
        </div>

        <RichTextEditor
          value={body}
          onChange={setBody}
          placeholder={tc("body")}
          onImagePaste={handleImagePaste}
        />

        {quoted && (
          <div className="rounded border border-slate-700 bg-slate-900/60">
            <button
              type="button"
              onClick={() => setQuoteOpen((o) => !o)}
              className="w-full px-3 py-1.5 text-left text-xs text-slate-400 hover:text-slate-200"
              title="Оригинал уйдёт вместе с ответом; править его нельзя — так он и дойдёт получателю в исходном виде"
            >
              ··· {quoteOpen ? "скрыть цитату" : "показать цитату"}
            </button>
            {quoteOpen && (
              <div
                className="max-h-64 overflow-auto border-t border-slate-700 px-3 py-2 text-xs text-slate-400 [&_table]:w-auto [&_td]:border [&_td]:border-slate-700 [&_td]:px-1 [&_img]:max-w-full"
                // Санитизированная копия с сервера — та же, что рендерится в
                // просмотре письма; сюда попадает только она.
                dangerouslySetInnerHTML={{ __html: quoted }}
              />
            )}
          </div>
        )}

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
              {aiBusy
                ? `${aiStep || "Агент работает"}… ${aiElapsed} с`
                : t("actions.aiHelp")}
            </button>
          </div>
        </div>

        {blocked.length > 0 && (
          <div className="rounded border border-red-800/70 bg-red-950/25 p-3">
            <p className="text-xs font-medium text-red-300">
              {t("sendBlocked")}
            </p>
            <ul className="mt-1 space-y-0.5">
              {blocked.map((b) => (
                <li key={b.code} className="text-xs text-red-200">
                  · {b.message}
                </li>
              ))}
            </ul>
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => handleSend(blocked.map((b) => b.code))}
                className="rounded border border-red-700 px-2 py-1 text-xs text-red-200 hover:bg-red-900/40"
              >
                {t("sendAnyway")}
              </button>
              <button
                onClick={() => setBlocked([])}
                className="rounded border border-slate-600 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
              >
                {t("fixIt")}
              </button>
            </div>
          </div>
        )}

        {error && <p className="text-xs text-red-400">{error}</p>}
      </div>

      {sentUndo && (
        <div className="flex items-center gap-3 border-t border-emerald-800/60 bg-emerald-950/25 px-4 py-2">
          <span className="text-xs text-emerald-300">
            Письмо отправляется… {undoLeft} с
          </span>
          <button
            onClick={handleUndo}
            className="rounded border border-emerald-700 px-2 py-0.5 text-xs text-emerald-200 hover:bg-emerald-900/40"
          >
            {t("undo")}
          </button>
        </div>
      )}

      <div className="flex items-center gap-2 border-t border-slate-700 px-4 py-2">
        <button
          onClick={() => handleSend()}
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
        <label className="cursor-pointer rounded border border-slate-600 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700">
          📎 {tc("attach")}
          <input type="file" multiple hidden onChange={(e) => handleUpload(e.target.files)} />
        </label>
        {native && (
          <button
            onClick={handleScan}
            disabled={busy}
            className="rounded border border-slate-600 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700 disabled:opacity-50"
            title="Снять документ камерой и приложить"
          >
            📷 Снять
          </button>
        )}
        {native && canDictate && (
          <button
            onClick={handleDictate}
            disabled={busy || listening}
            className="rounded border border-slate-600 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-700 disabled:opacity-50"
            title="Продиктовать текст письма"
          >
            {listening ? "🎙 Слушаю…" : "🎙 Диктовать"}
          </button>
        )}
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
