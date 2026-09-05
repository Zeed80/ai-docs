"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import { useAgentName } from "@/lib/agent-name";
import { useUserTimeZone } from "@/lib/user-time";
import type { EmailMessage, EmailThread } from "./types";

// Mirrors app/domain/email_triage.py — the taxonomy the backend acts on.
// Подписи переводятся: раньше они были зашиты по-русски в компоненте, хотя
// next-intl подключён и остальной интерфейс переводится.
const CATEGORY_KEYS = [
  "invoice", "quote", "document_request", "payment_question", "complaint",
  "contract", "notification", "newsletter", "personal", "other",
] as const;

const PERFORMED_KEYS = [
  "label", "notify_responsible", "link_invoice", "draft_reply",
  "compare_quote", "ask_for_attachment",
] as const;

function MessageCard({
  msg,
  defaultOpen,
  onReply,
  onForward,
}: {
  msg: EmailMessage;
  defaultOpen: boolean;
  /** Действия на КОНКРЕТНОМ письме: в шапке треда они были привязаны к
   *  последнему сообщению, и ответить на нужное письмо в длинной переписке
   *  было нельзя — уходили не та цитата и не тот In-Reply-To. */
  onReply: (m: EmailMessage, all?: boolean) => void;
  onForward: (m: EmailMessage) => void;
}) {
  const t = useTranslations("email");
  const timeZone = useUserTimeZone();
  const [open, setOpen] = useState(defaultOpen);
  const [status, setStatus] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  // Ф1.4 — доверие приходит с сервера: либо пользователь включил показ для
  // себя, либо этот отправитель у него в доверенных.
  const [showImages, setShowImages] = useState(!!msg.images_trusted);
  const [trusting, setTrusting] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const [savedContact, setSavedContact] = useState(false);

  async function addToContacts() {
    const raw = msg.from_address || "";
    const email = (raw.match(/<([^>]+)>/)?.[1] ?? raw).trim();
    const name = raw.replace(/\s*<[^>]+>\s*/, "").trim() || null;
    try {
      await emailApi.createContact({ email, name, upsert: true });
      setSavedContact(true);
    } catch {
      setSavedContact(false);
    }
  }
  const [triage, setTriage] = useState(msg.triage ?? null);

  async function correctCategory(category: string) {
    setCorrecting(true);
    try {
      const updated = await emailApi.correctTriage(msg.id, category);
      setTriage(updated);
    } finally {
      setCorrecting(false);
    }
  }
  const [frameHeight, setFrameHeight] = useState(320);
  const frameRef = useRef<HTMLIFrameElement>(null);

  const authBadge = useMemo(() => {
    const auth = msg.headers_meta?.auth;
    if (!auth || (!auth.spf && !auth.dkim)) return null;
    const bad = [auth.spf, auth.dkim].some((v) => v && v !== "pass");
    return bad
      ? { key: "authBad", cls: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300" }
      : {
          key: "authOk",
          cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
        };
  }, [msg.headers_meta]);

  const unsubscribeHref = useMemo(() => {
    const raw = msg.headers_meta?.list_unsubscribe ?? "";
    const match = raw.match(/<(https?:[^>]+)>/) || raw.match(/<(mailto:[^>]+)>/);
    return match ? match[1] : null;
  }, [msg.headers_meta]);

  // Инлайновые части (логотипы подписи) не вложения ни в каком смысле,
  // который человек имеет в виду.
  const visibleAttachments = useMemo(
    () => msg.attachments.filter((a) => !a.is_inline),
    [msg.attachments],
  );

  const measureFrame = useCallback(() => {
    const doc = frameRef.current?.contentDocument;
    if (!doc?.body) return;
    // A couple of passes: images finish loading after the first onLoad.
    const apply = () => {
      const h = Math.min(
        Math.max(doc.body.scrollHeight + 24, 120),
        2400,
      );
      setFrameHeight(h);
    };
    apply();
    setTimeout(apply, 250);
    setTimeout(apply, 1200);
  }, []);

  useEffect(() => {
    if (open) measureFrame();
  }, [open, showImages, measureFrame]);

  // Ф5.3 — печать письма, а не всей страницы с сайдбаром и списком тредов.
  // sandbox="allow-same-origin" (без allow-scripts) даёт родителю доступ к
  // contentWindow, поэтому печатать можно сам фрейм.
  const printLetter = useCallback(() => {
    const win = frameRef.current?.contentWindow;
    if (!win) return;
    try {
      win.focus();
      win.print();
    } catch {
      // Текстовые письма рендерятся без фрейма — печатаем страницу как есть.
      window.print();
    }
  }, []);

  const rawHtml = msg.body_html_sanitized || msg.body_html;
  // Ф1.4: the ingest pass parks remote image URLs in data-blocked-src so the
  // sender cannot use them as a read receipt. Showing them is a string swap
  // here — the iframe runs no scripts of its own.
  const blockedCount = useMemo(
    () => (rawHtml ? (rawHtml.match(/data-blocked-src=/g) ?? []).length : 0),
    [rawHtml],
  );

  const quotedLabel = t("quotedPrevious");
  const srcdoc = useMemo(() => {
    if (!rawHtml) return null;
    let html = showImages
      ? rawHtml.replace(/data-blocked-src=/g, "src=")
      : rawHtml;
    // Ф5.3 — свернуть цитату. В переписке из десяти писем каждое несёт девять
    // предыдущих, и полезная часть — первые три строки. <details> сворачивает
    // без единой строки скрипта, а скрипты внутри iframe и запрещены.
    html = html
      .replace(
        /<blockquote/gi,
        `<details class="q"><summary>${quotedLabel}</summary><blockquote`,
      )
      .replace(/<\/blockquote>/gi, "</blockquote></details>");
    // White canvas for the letter: senders style for a light background, and
    // forcing a light text colour on a transparent one made ordinary mail
    // unreadable (dark text on dark).
    return `<!doctype html><meta charset="utf-8"><base target="_blank"><style>html,body{background:#fff;color:#0f172a}body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:12px}a{color:#1d4ed8}img{max-width:100%}table{max-width:100%}details.q{margin:8px 0}details.q>summary{cursor:pointer;color:#64748b;font-size:12px;list-style:none}details.q>summary::-webkit-details-marker{display:none}details.q>summary::before{content:"··· ";letter-spacing:2px}details.q blockquote{border-left:2px solid #cbd5e1;margin:6px 0 0;padding-left:10px;color:#475569}</style>${html}`;
  }, [rawHtml, showImages, quotedLabel]);

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
      className={`rounded-lg border ${msg.is_inbound ? "border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800" : "border-blue-300 dark:border-blue-800 bg-blue-900/20"}`}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between gap-2 px-4 py-2.5 text-left"
      >
        <div className="flex min-w-0 items-center gap-2.5">
          <div
            className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
              msg.is_inbound ? "bg-slate-100 dark:bg-slate-700 text-slate-700 dark:text-slate-300" : "bg-blue-600 text-white"
            }`}
          >
            {(msg.from_address[0] ?? "?").toUpperCase()}
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm text-slate-900 dark:text-slate-100">{msg.from_address}</p>
            {!open && (
              <p className="truncate text-xs text-slate-400">
                {msg.snippet || (msg.body_text ?? "").slice(0, 90)}
              </p>
            )}
          </div>
        </div>
        <span className="shrink-0 text-[11px] text-slate-400">
          {new Date(msg.sent_at || msg.received_at || "").toLocaleString(undefined, {
            timeZone,
            day: "numeric",
            month: "short",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      </button>

      {open && (
        <div className="border-t border-slate-200 dark:border-slate-700 px-4 pb-4">
          {msg.to_addresses && (
            <p className="mt-2 text-xs text-slate-400">{t("toLabel")}: {msg.to_addresses.join(", ")}</p>
          )}

          {/* Ф1.2 verdicts, finally visible: the agent may create an invoice
              from this letter, so "кто это на самом деле" belongs on screen. */}
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[10px]">
            {authBadge && (
              <span className={`rounded px-1.5 py-0.5 ${authBadge.cls}`}>
                {t(authBadge.key)}
              </span>
            )}
            {msg.body_text_derived && (
              <span
                className="rounded bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 text-slate-700 dark:text-slate-300"
                title={t("textRecoveredHint")}
              >
                {t("textRecovered")}
              </span>
            )}
            {msg.headers_meta?.list_unsubscribe && (
              <a
                href={unsubscribeHref ?? "#"}
                target="_blank"
                rel="noreferrer"
                className="rounded bg-slate-100 dark:bg-slate-700 px-1.5 py-0.5 text-slate-700 dark:text-slate-300 hover:text-slate-900 dark:hover:text-slate-100"
              >
                {t("unsubscribe")}
              </a>
            )}
            <a
              href={emailApi.rawUrl(msg.id)}
              target="_blank"
              rel="noreferrer"
              className="ml-auto rounded px-1.5 py-0.5 text-slate-400 hover:text-slate-700 dark:hover:text-slate-300"
              title={t("downloadEmlHint")}
            >
              {t("downloadEml")}
            </a>
            <button
              onClick={printLetter}
              className="rounded px-1.5 py-0.5 text-slate-400 hover:text-slate-700 dark:hover:text-slate-700 dark:hover:text-slate-300"
              title={t("printLetterHint")}
            >
              {t("print")}
            </button>
          </div>

          {/* Действия на этом письме, а не на последнем в переписке. */}
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
            <button
              onClick={() => onReply(msg)}
              className="rounded border border-slate-300 px-2 py-0.5 text-slate-400 dark:text-slate-400 hover:bg-slate-100 dark:hover:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-100 dark:hover:bg-slate-700"
            >
              {t("actions.reply")}
            </button>
            <button
              onClick={() => onReply(msg, true)}
              className="rounded border border-slate-300 px-2 py-0.5 text-slate-400 dark:text-slate-400 hover:bg-slate-100 dark:hover:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-100 dark:hover:bg-slate-700"
            >
              {t("actions.replyAll")}
            </button>
            <button
              onClick={() => onForward(msg)}
              className="rounded border border-slate-300 px-2 py-0.5 text-slate-400 dark:text-slate-400 hover:bg-slate-100 dark:hover:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-100 dark:hover:bg-slate-700"
            >
              {t("actions.forward")}
            </button>
            {/* Завести контакт можно было только вручную в отдельном разделе,
                перепечатав адрес. */}
            <button
              onClick={addToContacts}
              disabled={savedContact}
              className="rounded border border-slate-300 px-2 py-0.5 text-slate-400 dark:text-slate-400 hover:bg-slate-100 disabled:opacity-60 dark:disabled:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-100 dark:hover:bg-slate-700"
            >
              {savedContact ? t("contactSaved") : t("actions.addContact")}
            </button>
            <a
              href={`/email?from=${encodeURIComponent(msg.from_address)}`}
              className="rounded px-2 py-0.5 text-slate-400 hover:text-slate-700 dark:hover:text-slate-700 dark:hover:text-slate-300"
            >
              {t("actions.allFromSender")}
            </a>
          </div>
          {srcdoc ? (
            <>
              {blockedCount > 0 && !showImages && (
                <div className="mt-3 flex items-center gap-2 rounded border border-amber-400 dark:border-amber-800/60 bg-amber-50 dark:bg-amber-950/20 px-3 py-1.5 text-xs text-amber-700 dark:text-amber-300">
                  <span>{t("imagesBlockedHint", { n: blockedCount })}</span>
                  <button
                    onClick={() => setShowImages(true)}
                    className="ml-auto rounded border border-amber-700 px-2 py-0.5 hover:bg-amber-100 dark:hover:bg-amber-900/40"
                  >
                    {t("showImages")}
                  </button>
                  <button
                    disabled={trusting}
                    onClick={async () => {
                      setTrusting(true);
                      try {
                        await emailApi.trustSenderImages(msg.from_address, null);
                        setShowImages(true);
                      } finally {
                        setTrusting(false);
                      }
                    }}
                    className="rounded border border-amber-700 px-2 py-0.5 hover:bg-amber-100 dark:hover:bg-amber-900/40 disabled:opacity-50"
                    title={t("trustSenderHint")}
                  >
                    {t("trustSender")}
                  </button>
                </div>
              )}
              <iframe
                ref={frameRef}
                // allow-same-origin (never allow-scripts): the parent must be
                // able to measure the rendered height. Ф5.3 — the frame was a
                // fixed 360 px, so a long letter scrolled inside a small box
                // inside the page's own scroll.
                sandbox="allow-same-origin"
                srcDoc={srcdoc}
                onLoad={measureFrame}
                className="mt-3 w-full rounded bg-white"
                style={{ height: frameHeight, border: 0 }}
              />
            </>
          ) : (
            <div className="mt-3 whitespace-pre-wrap text-sm leading-relaxed text-slate-800 dark:text-slate-200">
              {msg.body_text || <span className="italic text-slate-400">{t("emptyBody")}</span>}
            </div>
          )}

          {(msg.applied_rules?.length ?? 0) > 0 && (
            <div className="mt-3 rounded border border-slate-200 bg-slate-50 px-2.5 py-1.5 dark:border-slate-700 dark:bg-slate-800/60">
              {msg.applied_rules!.map((r) => (
                <p key={r.rule_id} className="text-[11px] text-slate-600 dark:text-slate-400">
                  {t("activityRule", { name: r.name })}
                  {r.actions.length > 0 && (
                    <span className="text-slate-400">
                      {" → "}
                      {r.actions
                        .map((a) =>
                          ["label", "add_label", "move", "assign_role", "run_extraction",
                           "forward_to", "auto_reply_template", "mark_read",
                           "notify_responsible", "link_invoice", "draft_reply",
                           "compare_quote", "ask_for_attachment"].includes(a)
                            ? t(`performed.${a}`)
                            : a,
                        )
                        .join(", ")}
                    </span>
                  )}
                  <a
                    href="/settings?tab=email&sub=rules"
                    className="ml-1.5 text-blue-600 hover:underline dark:text-blue-400"
                  >
                    {t("openRule")}
                  </a>
                </p>
              ))}
            </div>
          )}

          {triage && triage.status === "done" && (
            <div className="mt-3 rounded border border-sky-300 dark:border-sky-800/60 bg-sky-50 dark:bg-sky-950/20 p-2.5">
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-medium text-sky-700 dark:text-sky-300">
                  {t("triageBy", { category: triage.category_label })}
                </span>
                {triage.confidence != null && (
                  <span className="text-[10px] text-slate-400">
                    {t("confidence", { n: (triage.confidence * 100).toFixed(0) })}
                  </span>
                )}
                {triage.corrected_category && (
                  <span className="text-[10px] text-amber-600 dark:text-amber-400">
                    {t("correctedByHuman")}
                  </span>
                )}
              </div>
              {triage.summary && (
                <p className="mt-1 text-xs text-slate-700 dark:text-slate-300">{triage.summary}</p>
              )}
              {triage.performed.length > 0 && (
                <ul className="mt-1.5 space-y-0.5">
                  {triage.performed.map((a, i) => (
                    <li key={i} className="text-[11px] text-slate-400 dark:text-slate-400">
                      ✓ {(PERFORMED_KEYS as readonly string[]).includes(a.type)
                        ? t(`performed.${a.type}`)
                        : a.type}
                    </li>
                  ))}
                </ul>
              )}
              {triage.proposed.length > 0 && (
                <ul className="mt-1 space-y-0.5">
                  {triage.proposed.map((a, i) => (
                    <li key={i} className="text-[11px] text-amber-700 dark:text-amber-300">
                      · {t("proposes")}:{" "}
                      {a.hint ??
                        ((PERFORMED_KEYS as readonly string[]).includes(a.type)
                          ? t(`performed.${a.type}`)
                          : a.type)}
                    </li>
                  ))}
                </ul>
              )}
              <div className="mt-1.5 flex items-center gap-1.5">
                <span className="text-[10px] text-slate-400">{t("wrongCategory")}</span>
                <select
                  defaultValue=""
                  onChange={(e) => e.target.value && correctCategory(e.target.value)}
                  className="rounded bg-white dark:bg-slate-800 px-1 py-0.5 text-[10px] text-slate-700 dark:text-slate-300"
                >
                  <option value="">{t("correct")}…</option>
                  {CATEGORY_KEYS.map((k) => (
                    <option key={k} value={k}>
                      {t(`categories.${k}`)}
                    </option>
                  ))}
                </select>
                {correcting && <span className="text-[10px] text-slate-400">…</span>}
              </div>
            </div>
          )}

          {(msg.derived_invoices?.length ?? 0) > 0 && (
            <div className="mt-3 rounded border border-emerald-300 dark:border-emerald-800/60 bg-emerald-50 dark:bg-emerald-950/20 p-2.5">
              <p className="text-[11px] font-medium text-emerald-700 dark:text-emerald-300">
                {t("derivedInvoices")}
              </p>
              <ul className="mt-1.5 space-y-1">
                {msg.derived_invoices!.map((inv) => (
                  <li key={inv.invoice_id} className="text-xs text-slate-800 dark:text-slate-200">
                    <a
                      href={`/invoices/${inv.invoice_id}`}
                      className="text-emerald-700 dark:text-emerald-300 hover:underline"
                    >
                      {t("invoiceNo", { number: inv.invoice_number ?? t("noNumber") })}
                    </a>
                    {inv.total_amount != null && (
                      <span className="text-slate-400 dark:text-slate-400">
                        {" · "}
                        {inv.total_amount.toLocaleString("ru-RU")} {inv.currency ?? "RUB"}
                      </span>
                    )}
                    <span className="text-slate-400">
                      {" · "}
                      {inv.status === "needs_review" ? t("needsReview") : inv.status}
                    </span>
                    {inv.supplier_name && (
                      <span className="block text-[11px] text-slate-400">
                        {t("supplierLabel")}: {inv.supplier_name}
                        {inv.supplier_matched_by &&
                          ["email_sender_exact", "email_sender_domain", "inn", "created"].includes(
                            inv.supplier_matched_by,
                          ) &&
                          ` (${t(`supplierBy.${inv.supplier_matched_by}`)})`}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {visibleAttachments.length > 0 && (
            <div className="mt-3 space-y-1.5">
              {visibleAttachments.length > 1 && (
                <div className="flex justify-end">
                  <a
                    href={emailApi.attachmentsArchiveUrl(msg.id)}
                    className="rounded border border-slate-300 dark:border-slate-600 px-2 py-0.5 text-[11px] text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700"
                  >
                    {t("downloadAll", { n: visibleAttachments.length })}
                  </a>
                </div>
              )}
              {visibleAttachments.map((a) => (
                <div
                  key={a.id}
                  className="flex flex-wrap items-center gap-2 rounded border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 px-3 py-1.5"
                >
                  {isPreviewable(a) ? (
                    <button
                      onClick={() =>
                        setPreview((p) => (p === a.id ? null : a.id))
                      }
                      className="shrink-0 text-xs text-slate-400 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200"
                      title={preview === a.id ? t("collapseAttachment") : t("expandAttachment")}
                    >
                      {preview === a.id ? "▾" : "▸"}
                    </button>
                  ) : (
                    <span className="shrink-0 text-xs text-slate-400 dark:text-slate-400">·</span>
                  )}
                  <span className="shrink-0 text-sm" aria-hidden>
                    {fileIcon(a)}
                  </span>
                  <a
                    href={emailApi.attachmentUrl(msg.id, a.filename)}
                    target="_blank"
                    rel="noreferrer"
                    className="truncate text-xs font-mono text-blue-700 dark:text-blue-300 hover:underline"
                  >
                    {a.filename}
                  </a>
                  <span className="shrink-0 text-[11px] text-slate-400">
                    {humanSize(a.size)}
                  </span>
                  <button
                    onClick={() => process(a.filename, "document")}
                    disabled={busy === a.filename}
                    className="ml-auto rounded border border-slate-300 dark:border-slate-600 px-2 py-0.5 text-xs hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50"
                  >
                    {t("attachmentTo.document")}
                  </button>
                  <button
                    onClick={() => process(a.filename, "drawing")}
                    disabled={busy === a.filename}
                    className="rounded border border-slate-300 dark:border-slate-600 px-2 py-0.5 text-xs hover:bg-slate-100 dark:hover:bg-slate-700 disabled:opacity-50"
                  >
                    {t("attachmentTo.drawing")}
                  </button>
                  {status[a.filename] && (
                    <span className="w-full text-[11px] text-slate-400 dark:text-slate-400">{status[a.filename]}</span>
                  )}
                  {preview === a.id && (
                    // ?disposition=inline честен только для типов из белого
                    // списка бэкенда — всё остальное придёт как загрузка.
                    <div className="w-full">
                      {(a.content_type ?? "").startsWith("image/") ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img
                          src={`${emailApi.attachmentUrl(msg.id, a.filename)}?disposition=inline`}
                          alt={a.filename}
                          className="max-h-96 rounded border border-slate-200 dark:border-slate-700 bg-white"
                        />
                      ) : (
                        <iframe
                          title={a.filename}
                          src={`${emailApi.attachmentUrl(msg.id, a.filename)}?disposition=inline`}
                          className="h-96 w-full rounded border border-slate-200 dark:border-slate-700 bg-white"
                        />
                      )}
                    </div>
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

/** Ф5.3 — голое имя файла не говорит ни размера, ни типа, ни что внутри. */
const PREVIEWABLE = /^(image\/(png|jpeg|gif|webp|bmp)|application\/pdf|text\/plain)$/;

function isPreviewable(a: { content_type: string | null }): boolean {
  return PREVIEWABLE.test((a.content_type ?? "").split(";")[0].trim().toLowerCase());
}

function humanSize(bytes: number | null): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} КБ`;
  return `${(bytes / 1024 / 1024).toFixed(1)} МБ`;
}

function fileIcon(a: { filename: string; content_type: string | null }): string {
  const type = (a.content_type ?? "").toLowerCase();
  const ext = a.filename.split(".").pop()?.toLowerCase() ?? "";
  if (type.startsWith("image/")) return "🖼";
  if (type === "application/pdf" || ext === "pdf") return "📕";
  if (["xls", "xlsx", "csv", "ods"].includes(ext)) return "📊";
  if (["doc", "docx", "rtf", "odt"].includes(ext)) return "📝";
  if (["zip", "rar", "7z", "tar", "gz"].includes(ext)) return "🗜";
  if (["dwg", "dxf", "step", "stp", "iges", "igs"].includes(ext)) return "📐";
  return "📎";
}

export function ThreadView({
  threadId,
  onReply,
  onForward,
  onRead,
  onArchive,
  onTrash,
  onClose,
}: {
  threadId: string;
  onReply: (m: EmailMessage, all?: boolean, assist?: string) => void;
  onForward: (m: EmailMessage) => void;
  /** Прочтение помечалось молча: список слева оставлял письмо жирным, а
   *  счётчик непрочитанных не менялся до перезагрузки. */
  onRead?: (threadId: string) => void;
  onArchive: () => void;
  onTrash: () => void;
  onClose: () => void;
}) {
  const t = useTranslations("email");
  const agentName = useAgentName();
  const [thread, setThread] = useState<EmailThread | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setThread(null);
    setError(null);
    emailApi
      .thread(threadId)
      .then((th) => {
        setThread(th);
        if (!th.is_read) {
          emailApi
            .bulkAction([th.id], "read")
            .then(() => onRead?.(th.id))
            .catch(() => {});
        }
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId]);

  if (error) return <div className="p-6 text-sm text-red-500 dark:text-red-400">{error}</div>;
  if (!thread) return <div className="p-6 text-sm text-slate-400 dark:text-slate-400">…</div>;

  const last = thread.messages[thread.messages.length - 1];

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-slate-200 dark:border-slate-700 px-4 py-2.5">
        {/* Ф7.2 — on a phone the thread replaces the list, so "назад" is the
            only way out; on desktop both panes are visible and it is noise. */}
        <button
          onClick={onClose}
          aria-label={t("backToList")}
          className="text-lg text-slate-400 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200 md:hidden"
        >
          ←
        </button>
        <h2 className="flex-1 truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
          {thread.subject || t("noSubject")}
        </h2>
        <button
          onClick={() => last && onReply(last)}
          className="rounded px-2 py-1 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700"
        >
          {t("actions.reply")}
        </button>
        {/* Ф6.9 — черновик готовит агент, отправляет человек: гейт на
            email.send никуда не девается. */}
        <button
          onClick={() =>
            last && onReply(last, false, t("assistPrompt"))
          }
          className="rounded border border-sky-800 bg-sky-950/30 px-3 py-1.5 text-sm text-sky-700 dark:text-sky-300 hover:bg-sky-900/40"
          title={t("assistHint")}
        >
          {agentName}, ответь
        </button>
        <button
          onClick={() => last && onReply(last, true)}
          className="rounded px-2 py-1 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700"
        >
          {t("actions.replyAll")}
        </button>
        <button
          onClick={() => last && onForward(last)}
          className="rounded px-2 py-1 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700"
        >
          {t("actions.forward")}
        </button>
        <button
          onClick={onArchive}
          className="rounded px-2 py-1 text-xs text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-700"
        >
          {t("actions.archive")}
        </button>
        <button
          onClick={onTrash}
          className="rounded px-2 py-1 text-xs text-slate-400 dark:text-slate-400 hover:bg-red-50 dark:hover:bg-red-900/40 hover:text-red-600 dark:hover:text-red-300"
        >
          {t("actions.trash")}
        </button>
      </div>
      <div className="flex-1 space-y-3 overflow-auto p-4">
        {thread.messages.map((m, i) => (
          <MessageCard
            key={m.id}
            msg={m}
            defaultOpen={i === thread.messages.length - 1}
            onReply={onReply}
            onForward={onForward}
          />
        ))}
      </div>
    </div>
  );
}
