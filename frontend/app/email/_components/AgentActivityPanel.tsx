"use client";

import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import { useUserTimeZone } from "@/lib/user-time";

type Activity = Awaited<ReturnType<typeof emailApi.agentActivity>>[number];

const PERFORMED_KEYS = [
  "label", "notify_responsible", "link_invoice", "draft_reply",
  "compare_quote", "ask_for_attachment", "add_label", "move", "assign_role",
  "run_extraction", "forward_to", "auto_reply_template", "mark_read",
] as const;

/**
 * Лента «что агент и правила сделали с почтой», с отменой.
 *
 * Автономию принимают, когда её видно и можно откатить. Разбор письма был
 * виден только внутри треда, сработавшее правило не показывалось нигде, а
 * снять поставленную агентом метку можно было лишь вручную, не понимая, кто
 * её поставил.
 */
export function AgentActivityPanel({ mailbox }: { mailbox: string }) {
  const t = useTranslations("email");
  const timeZone = useUserTimeZone();
  const [rows, setRows] = useState<Activity[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [undone, setUndone] = useState<Set<string>>(new Set());

  const load = useCallback(() => {
    emailApi
      .agentActivity(mailbox || undefined)
      .then(setRows)
      .catch(() => setRows([]));
  }, [mailbox]);

  useEffect(load, [load]);

  async function undo(row: Activity) {
    setBusy(row.id);
    try {
      await emailApi.undoAgentAction(row.id, row.kind);
      setUndone((prev) => new Set(prev).add(row.id));
    } finally {
      setBusy(null);
    }
  }

  const actionLabel = (type?: string) =>
    type && (PERFORMED_KEYS as readonly string[]).includes(type)
      ? t(`performed.${type}`)
      : type ?? "";

  return (
    <div className="flex-1 overflow-auto bg-white p-4 dark:bg-slate-900">
      <h2 className="mb-3 text-sm font-semibold text-slate-900 dark:text-slate-100">
        {t("agentActivity")}
      </h2>
      {rows.length === 0 && (
        <p className="text-xs text-slate-500">{t("empty.filtered")}</p>
      )}
      <ul className="space-y-1.5">
        {rows.map((row) => (
          <li
            key={`${row.kind}-${row.id}`}
            className="rounded border border-slate-200 bg-slate-50 px-3 py-2 dark:border-slate-700 dark:bg-slate-800/60"
          >
            <div className="flex items-baseline gap-2">
              <span className="text-[11px] font-medium text-slate-700 dark:text-slate-300">
                {row.kind === "rule"
                  ? t("activityRule", { name: row.source })
                  : t("activityAgent")}
              </span>
              <span className="text-[10px] text-slate-500">
                {new Date(row.at).toLocaleString(undefined, { timeZone })}
              </span>
              {row.undoable.length > 0 && !undone.has(row.id) && (
                <button
                  onClick={() => void undo(row)}
                  disabled={busy === row.id}
                  className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-[10px] text-slate-600 hover:bg-slate-100 disabled:opacity-50 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-700"
                >
                  {t("undo")}
                </button>
              )}
              {undone.has(row.id) && (
                <span className="ml-auto text-[10px] text-emerald-600 dark:text-emerald-400">
                  {t("activityUndone")}
                </span>
              )}
            </div>
            {row.thread_id ? (
              <a
                href={`/email/${row.thread_id}`}
                className="block truncate text-xs text-blue-600 hover:underline dark:text-blue-400"
              >
                {row.subject || t("noSubject")}
              </a>
            ) : (
              <span className="block truncate text-xs text-slate-700 dark:text-slate-300">
                {row.subject || t("noSubject")}
              </span>
            )}
            {row.summary && (
              <p className="truncate text-[11px] text-slate-500">{row.summary}</p>
            )}
            {row.performed.length > 0 && (
              <p className="text-[10px] text-slate-500">
                {row.performed
                  .map((a) => actionLabel(a.type as string | undefined))
                  .filter(Boolean)
                  .join(" · ")}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
