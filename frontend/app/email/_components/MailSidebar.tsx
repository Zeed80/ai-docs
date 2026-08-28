"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import type { EmailLabel, MailboxChip } from "./types";

const FOLDERS = ["inbox", "sent", "archive", "spam", "trash"] as const;

export function MailSidebar({
  onCollapse,
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

  const totalUnread = mailboxes.reduce((n, m) => n + (m.unread_count || 0), 0);
  const activeChip = mailboxes.find((m) => m.name === activeMailbox) ?? null;

  const rowCls = (on: boolean) =>
    `flex items-center justify-between rounded px-2 py-1.5 text-sm cursor-pointer ${
      on ? "bg-blue-900/40 text-blue-200" : "text-slate-300 hover:bg-slate-800"
    }`;

  return (
    <div className="flex w-56 shrink-0 flex-col border-r border-slate-700 bg-slate-900/60">
      <div className="p-2">
        <div className="mb-2 flex gap-1">
          <button
            onClick={onSync}
            disabled={syncing}
            className="flex-1 rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700 disabled:opacity-50"
          >
            {syncing ? t("syncing") : t("syncNow")}
          </button>
          <button
            onClick={onCollapse}
            className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-400 hover:bg-slate-700"
            title="Свернуть"
          >
            «
          </button>
        </div>
        {activeChip?.sync_error && (
          <p className="mb-2 px-1 text-[10px] text-red-400">
            {t("syncError", { error: activeChip.sync_error.slice(0, 60) })}
          </p>
        )}
      </div>

      <div className="flex-1 overflow-auto px-2 pb-4">
        <p className="px-2 pb-1 pt-2 text-[10px] uppercase tracking-wide text-slate-500">
          {t("title")}
        </p>
        <div
          className={rowCls(activeMailbox === "" && !starredOnly && !activeLabel)}
          onClick={() => onSelectMailbox("")}
        >
          <span>{t("folders.inbox")}</span>
          {totalUnread > 0 && <span className="text-xs text-blue-300">{totalUnread}</span>}
        </div>
        {mailboxes.map((m) => (
          <div
            key={m.name}
            className={rowCls(activeMailbox === m.name)}
            onClick={() => onSelectMailbox(m.name)}
          >
            <span className="flex items-center gap-1 truncate">
              {m.sync_error && <span className="h-1.5 w-1.5 rounded-full bg-red-500" />}
              {m.display_name || m.name}
            </span>
            {m.unread_count > 0 && (
              <span className="text-xs text-blue-300">{m.unread_count}</span>
            )}
          </div>
        ))}

        <p className="px-2 pb-1 pt-3 text-[10px] uppercase tracking-wide text-slate-500">
          {t("folders.starred")} / {t("labels")}
        </p>
        <div className={rowCls(starredOnly)} onClick={onToggleStarred}>
          <span>★ {t("folders.starred")}</span>
        </div>
        {FOLDERS.filter((f) => f !== "inbox").map((f) => (
          <div
            key={f}
            className={rowCls(activeFolder === f && !starredOnly)}
            onClick={() => onSelectFolder(f)}
          >
            <span>{t(`folders.${f}`)}</span>
          </div>
        ))}

        {labels.map((l) => (
          <div
            key={l.id}
            className={rowCls(activeLabel === l.id)}
            onClick={() => onSelectLabel(l.id)}
          >
            <span className="flex items-center gap-1.5 truncate">
              <span
                className="h-2 w-2 rounded-full"
                style={{ background: l.color ?? "#64748b" }}
              />
              {l.name}
            </span>
            <span className="flex items-center gap-1">
              {l.thread_count > 0 && (
                <span className="text-xs text-slate-500">{l.thread_count}</span>
              )}
              {!l.is_system && (
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    emailApi.deleteLabel(l.id).then(onLabelsChanged);
                  }}
                  className="text-slate-600 hover:text-red-400"
                >
                  ×
                </button>
              )}
            </span>
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
            className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-200 placeholder-slate-600"
          />
        </form>
      </div>
    </div>
  );
}
