"use client";

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { EmailThread } from "./types";

function relDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString())
    return d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  if (d.getFullYear() === now.getFullYear())
    return d.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
  return d.toLocaleDateString("ru-RU");
}

/** Заголовок группы: список без разделителей по датам читается как одна
 *  сплошная лента — «Сегодня / Вчера» есть во всех современных клиентах. */
function dateBucket(iso: string | null, t: (k: string) => string): string {
  if (!iso) return t("groups.older");
  const d = new Date(iso);
  const now = new Date();
  const startOfDay = (x: Date) =>
    new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOfDay(now) - startOfDay(d)) / 86_400_000);
  if (days <= 0) return t("groups.today");
  if (days === 1) return t("groups.yesterday");
  if (days <= 7) return t("groups.thisWeek");
  if (days <= 30) return t("groups.thisMonth");
  return t("groups.older");
}

function initials(s: string): string {
  const parts = s.replace(/[<>"]/g, "").trim().split(/[\s@.]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}

function colorFor(s: string): string {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return `hsl(${h} 45% 45%)`;
}

export function ThreadList({
  threads,
  folder = "inbox",
  dense = false,
  loading,
  selectedId,
  selected,
  onOpen,
  onToggleSelect,
  onStar,
  emptyState,
  onLoadMore,
  loadingMore = false,
  hasMore = false,
}: {
  threads: EmailThread[];
  /** Папка нужна, чтобы в «Отправленных» показывать получателя, а не себя. */
  folder?: string;
  /** Компактный режим: больше писем на экран. */
  dense?: boolean;
  loading: boolean;
  selectedId: string | null;
  selected: Set<string>;
  onOpen: (t: EmailThread) => void;
  onToggleSelect: (id: string) => void;
  onStar: (t: EmailThread) => void;
  emptyState: React.ReactNode;
  /** Ф5.1 — infinite scroll; absent when there is nothing more to fetch. */
  onLoadMore?: () => void;
  loadingMore?: boolean;
  hasMore?: boolean;
}) {
  const t = useTranslations("email");
  const parentRef = useRef<HTMLDivElement>(null);
  const rows = useVirtualizer({
    count: threads.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => (dense ? 56 : 82),
    overscan: 8,
  });

  // Ф5.1 — fetch the next page as the last rows come into view. The list was
  // already virtualized, but nothing ever asked the server for more, so it was
  // a fast window onto a single page.
  const items = rows.getVirtualItems();
  const lastIndex = items.length ? items[items.length - 1].index : 0;
  useEffect(() => {
    if (hasMore && !loadingMore && onLoadMore && lastIndex >= threads.length - 5) {
      onLoadMore();
    }
  }, [lastIndex, threads.length, hasMore, loadingMore, onLoadMore]);

  if (loading)
    return (
      <div className="py-8 text-center text-sm text-slate-500" role="status" aria-live="polite">
        {t("loading")}
      </div>
    );
  if (threads.length === 0)
    return (
      <div className="px-4 py-10 text-center text-sm text-slate-500 dark:text-slate-400">
        {emptyState}
      </div>
    );

  return (
    <div ref={parentRef} className="h-full overflow-auto" role="listbox" aria-label={t("title")}>
      <div style={{ height: rows.getTotalSize(), position: "relative" }}>
        {items.map((vi) => {
          const th = threads[vi.index];
          const unread = !th.is_read;
          // Во «Входящих» собеседник — отправитель, в «Отправленных» и
          // «Черновиках» — получатель: там sender это мы сами, и список
          // выглядел как переписка с собой.
          const who =
            (["sent", "drafts", "outbox"].includes(folder)
              ? th.counterparty ?? th.sender
              : th.sender) ?? "—";
          const prev = vi.index > 0 ? threads[vi.index - 1] : null;
          const bucket = dateBucket(th.last_message_at, t);
          const showBucket =
            !prev || dateBucket(prev.last_message_at, t) !== bucket;
          return (
            <div
              key={th.id}
              data-index={vi.index}
              ref={rows.measureElement}
              style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
            >
              {showBucket && (
                <div className="bg-slate-100/80 px-3 py-1 text-[10px] font-medium uppercase tracking-wide text-slate-500 dark:bg-slate-900/70 dark:text-slate-500">
                  {bucket}
                </div>
              )}
              <div
                role="option"
                aria-selected={th.id === selectedId}
                tabIndex={0}
                onClick={() => onOpen(th)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onOpen(th);
                  }
                  if (e.key === "x") {
                    e.preventDefault();
                    onToggleSelect(th.id);
                  }
                }}
                className={`group flex cursor-pointer gap-2 border-b border-slate-200 px-3 ${dense ? "py-1.5" : "py-2.5"} transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500 dark:focus-visible:border-slate-800 ${
                  th.id === selectedId
                    ? "border-l-2 border-l-blue-500 bg-blue-50 dark:bg-blue-900/30"
                    : selected.has(th.id)
                      ? "bg-blue-50/60 dark:bg-blue-900/15"
                      : "border-l-2 border-l-transparent hover:bg-slate-100 dark:hover:bg-slate-50 dark:hover:bg-slate-800/60"
                }`}
              >
                <input
                  type="checkbox"
                  checked={selected.has(th.id)}
                  aria-label={t("selectThread")}
                  onClick={(e) => e.stopPropagation()}
                  onChange={() => onToggleSelect(th.id)}
                  // Раньше чекбокс проявлялся только по наведению: на планшете
                  // hover не наступает, и множественный выбор был недоступен.
                  className="mt-1 h-3.5 w-3.5 shrink-0 accent-blue-600 opacity-60 transition-opacity group-hover:opacity-100 checked:opacity-100 focus-visible:opacity-100"
                />
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onStar(th);
                  }}
                  aria-label={th.is_starred ? t("actions.unstar") : t("actions.star")}
                  aria-pressed={th.is_starred}
                  className={`mt-0.5 shrink-0 text-sm ${th.is_starred ? "text-amber-500" : "text-slate-500 dark:text-slate-400 hover:text-slate-600 dark:hover:text-slate-400 dark:hover:text-slate-600 dark:text-slate-600 dark:hover:text-slate-500 dark:hover:text-slate-400"}`}
                >
                  {th.is_starred ? "★" : "☆"}
                </button>
                {/* Аватар: в карточке письма и в подсказке получателей он уже
                    есть, а в самом просматриваемом списке его не было. */}
                <span
                  aria-hidden
                  className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[10px] font-bold text-white"
                  style={{ background: colorFor(who) }}
                >
                  {initials(who)}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className={`truncate text-sm ${unread ? "font-semibold text-slate-900 dark:text-slate-100" : "text-slate-700 dark:text-slate-300"}`}>
                      {who}
                    </span>
                    <span className="shrink-0 text-[11px] text-slate-500">{relDate(th.last_message_at)}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className={`truncate text-xs ${unread ? "text-slate-800 dark:text-slate-200" : "text-slate-500 dark:text-slate-400"}`}>
                      {th.subject || t("noSubject")}
                    </span>
                    {th.message_count > 1 && (
                      <span className="text-[10px] text-slate-500">{th.message_count}</span>
                    )}
                    {th.unread_count > 1 && (
                      <span className="rounded-full bg-blue-600 px-1.5 text-[9px] font-medium text-white">
                        {th.unread_count}
                      </span>
                    )}
                    {th.has_attachments && (
                      <span className="text-[10px]" role="img" aria-label={t("hasAttachments")}>
                        📎
                      </span>
                    )}
                    {th.has_draft && (
                      <span
                        className="rounded bg-sky-100 px-1 text-[9px] text-sky-700 dark:bg-sky-900/50 dark:text-sky-300"
                        title={t("draftWaitingHint")}
                      >
                        {t("draftWaiting")}
                      </span>
                    )}
                  </div>
                  {th.last_snippet && !dense && (
                    <p className="truncate text-[11px] text-slate-500">{th.last_snippet}</p>
                  )}
                  {th.labels.length > 0 && (
                    <div className="mt-0.5 flex flex-wrap gap-1">
                      {th.labels.map((l) => (
                        <span
                          key={l.id}
                          className="rounded-full px-1.5 text-[9px]"
                          style={{ background: (l.color ?? "#475569") + "33", color: l.color ?? "#64748b" }}
                        >
                          {l.name}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
      {loadingMore && (
        <p className="py-3 text-center text-xs text-slate-500">{t("loadingMore")}</p>
      )}
      {!hasMore && threads.length > 20 && (
        <p className="py-3 text-center text-xs text-slate-400 dark:text-slate-600">{t("allLoaded")}</p>
      )}
    </div>
  );
}
