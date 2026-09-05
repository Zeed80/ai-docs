"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import type { EmailLabel, MailboxChip } from "./types";

// Ф5.1 — "drafts" and "outbox" were missing entirely: a saved draft vanished
// (emailApi.drafts() existed and was called from nowhere), and a delayed send
// had no home either.
const FOLDERS = ["inbox", "drafts", "outbox", "sent", "archive", "spam", "trash"] as const;

export function MailSidebar({
  onCollapse,
  view,
  onSelectContacts,
  onSelectActivity,
  mailboxes,
  labels,
  activeMailbox,
  activeFolder,
  activeLabel,
  starredOnly,
  onSelectMailbox,
  onSelectFolder,
  onSelectLabel,
  onToggleStarred,
  onLabelsChanged,
  syncing,
  onSync,
}: {
  onCollapse: () => void;
  view: "mail" | "contacts" | "activity";
  onSelectContacts: () => void;
  onSelectActivity: () => void;
  mailboxes: MailboxChip[];
  labels: EmailLabel[];
  activeMailbox: string;
  activeFolder: string;
  activeLabel: string | null;
  starredOnly: boolean;
  onSelectMailbox: (name: string) => void;
  onSelectFolder: (f: string) => void;
  onSelectLabel: (id: string | null) => void;
  onToggleStarred: () => void;
  onLabelsChanged: () => void;
  syncing: boolean;
  onSync: () => void;
}) {
  const t = useTranslations("email");
  const [newLabel, setNewLabel] = useState("");
  const [fcounts, setFcounts] = useState<Record<string, { total: number; unread: number }>>({});

  useEffect(() => {
    emailApi
      .folderCounts(activeMailbox || undefined)
      .then((rows) => {
        const m: Record<string, { total: number; unread: number }> = {};
        for (const r of rows) m[r.folder] = { total: r.total, unread: r.unread };
        setFcounts(m);
      })
      .catch(() => setFcounts({}));
  }, [activeMailbox, mailboxes]);

  const totalUnread = mailboxes.reduce((n, m) => n + (m.unread_count || 0), 0);
  const activeChip = mailboxes.find((m) => m.name === activeMailbox) ?? null;

  // Каждый пункт был <div onClick>: недостижим с клавиатуры и невидим для
  // скринридера. Кнопка даёт фокус, Enter/Space и роль бесплатно.
  const rowCls = (on: boolean) =>
    `flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${
      on
        ? "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200"
        : "text-slate-700 hover:bg-slate-100 dark:hover:text-slate-300 dark:hover:bg-white dark:hover:bg-slate-800"
    }`;

  const countCls = "text-xs text-blue-600 dark:text-blue-300";

  return (
    <nav
      aria-label={t("foldersTitle")}
      className="flex w-56 shrink-0 flex-col border-r border-slate-200 bg-slate-50 dark:border-slate-700 dark:bg-slate-900/60"
    >
      <div className="p-2">
        <div className="mb-2 flex gap-1">
          <button
            onClick={onSync}
            disabled={syncing}
            className="flex-1 rounded bg-slate-200 px-2 py-1 text-xs text-slate-700 hover:bg-slate-300 disabled:opacity-50 dark:disabled:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-100 dark:hover:bg-slate-700"
          >
            {syncing ? t("syncing") : t("syncNow")}
          </button>
          <button
            onClick={onCollapse}
            aria-label={t("collapseSidebar")}
            className="rounded bg-slate-200 px-2 py-1 text-xs text-slate-500 hover:bg-slate-300 dark:hover:bg-slate-800 dark:text-slate-400 dark:hover:bg-slate-100 dark:hover:bg-slate-700"
            title={t("collapseSidebar")}
          >
            «
          </button>
        </div>
        {activeChip?.sync_error && (
          <p className="mb-2 px-1 text-[10px] text-red-500 dark:text-red-400">
            {t("syncError", { error: activeChip.sync_error.slice(0, 60) })}
          </p>
        )}
      </div>

      <div className="flex-1 overflow-auto px-2 pb-4">
        <p className="px-2 pb-1 pt-2 text-[10px] uppercase tracking-wide text-slate-500">
          {t("title")}
        </p>
        <button
          type="button"
          aria-current={activeMailbox === "" && !starredOnly && !activeLabel ? "page" : undefined}
          className={rowCls(activeMailbox === "" && !starredOnly && !activeLabel)}
          onClick={() => onSelectMailbox("")}
        >
          <span>{t("folders.inbox")}</span>
          {totalUnread > 0 && <span className={countCls}>{totalUnread}</span>}
        </button>
        {mailboxes.map((m) => (
          <button
            key={m.name}
            type="button"
            aria-current={activeMailbox === m.name ? "page" : undefined}
            className={rowCls(activeMailbox === m.name)}
            onClick={() => onSelectMailbox(m.name)}
          >
            <span className="flex items-center gap-1 truncate">
              {m.sync_error && (
                <span
                  className="h-1.5 w-1.5 rounded-full bg-red-500"
                  role="img"
                  aria-label={t("syncBrokenShort")}
                />
              )}
              {m.display_name || m.name}
            </span>
            {m.unread_count > 0 && <span className={countCls}>{m.unread_count}</span>}
          </button>
        ))}

        <p className="px-2 pb-1 pt-3 text-[10px] uppercase tracking-wide text-slate-500">
          {t("folders.starred")} / {t("labels")}
        </p>
        <button
          type="button"
          aria-pressed={starredOnly}
          className={rowCls(starredOnly)}
          onClick={onToggleStarred}
        >
          <span>★ {t("folders.starred")}</span>
        </button>
        {FOLDERS.filter((f) => f !== "inbox").map((f) => (
          <button
            key={f}
            type="button"
            aria-current={activeFolder === f && !starredOnly ? "page" : undefined}
            className={rowCls(activeFolder === f && !starredOnly)}
            onClick={() => onSelectFolder(f)}
          >
            <span>{t(`folders.${f}`)}</span>
            {fcounts[f]?.unread > 0 ? (
              <span className={countCls}>{fcounts[f].unread}</span>
            ) : fcounts[f]?.total > 0 && f !== "trash" ? (
              <span className="text-xs text-slate-500 dark:text-slate-400">{fcounts[f].total}</span>
            ) : null}
          </button>
        ))}

        {labels.map((l) => (
          <div key={l.id} className="flex items-center">
            <button
              type="button"
              aria-current={activeLabel === l.id ? "page" : undefined}
              className={rowCls(activeLabel === l.id)}
              onClick={() => onSelectLabel(l.id)}
            >
              <span className="flex items-center gap-1.5 truncate">
                <span
                  aria-hidden
                  className="h-2 w-2 rounded-full"
                  style={{ background: l.color ?? "#64748b" }}
                />
                {l.name}
              </span>
              {l.thread_count > 0 && (
                <span className="text-xs text-slate-500">{l.thread_count}</span>
              )}
            </button>
            {!l.is_system && (
              <button
                type="button"
                aria-label={t("deleteLabel", { name: l.name })}
                onClick={() => emailApi.deleteLabel(l.id).then(onLabelsChanged)}
                className="px-1 text-slate-500 dark:text-slate-400 hover:text-red-500 dark:hover:text-red-400"
              >
                ×
              </button>
            )}
          </div>
        ))}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!newLabel.trim()) return;
            emailApi.createLabel(newLabel.trim()).then(() => {
              setNewLabel("");
              onLabelsChanged();
            });
          }}
          className="mt-1 px-2"
        >
          <input
            value={newLabel}
            onChange={(e) => setNewLabel(e.target.value)}
            placeholder={t("newLabel")}
            aria-label={t("newLabel")}
            className="w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-800 placeholder-slate-400 dark:border-slate-700 dark:bg-slate-800 dark:text-slate-200 dark:placeholder-slate-600"
          />
        </form>

        <p className="px-2 pb-1 pt-3 text-[10px] uppercase tracking-wide text-slate-500">
          {t("more")}
        </p>
        <button
          type="button"
          aria-current={view === "contacts" ? "page" : undefined}
          className={rowCls(view === "contacts")}
          onClick={onSelectContacts}
        >
          <span>👤 {t("contacts")}</span>
        </button>
        <button
          type="button"
          aria-current={view === "activity" ? "page" : undefined}
          className={rowCls(view === "activity")}
          onClick={onSelectActivity}
        >
          <span>🤖 {t("agentActivity")}</span>
        </button>
      </div>
    </nav>
  );
}
