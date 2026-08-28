"use client";

import { useRef } from "react";
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

export function ThreadList({
  threads,
  loading,
  selectedId,
  selected,
  onOpen,
  onToggleSelect,
  onStar,
  emptyState,
}: {
  threads: EmailThread[];
  loading: boolean;
  selectedId: string | null;
  selected: Set<string>;
  onOpen: (t: EmailThread) => void;
  onToggleSelect: (id: string) => void;
  onStar: (t: EmailThread) => void;
  emptyState: React.ReactNode;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const rows = useVirtualizer({
    count: threads.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 82,
    overscan: 8,
  });

  if (loading)
    return <div className="py-8 text-center text-sm text-slate-400">…</div>;
  if (threads.length === 0)
    return <div className="py-10 px-4 text-center text-sm text-slate-400">{emptyState}</div>;

  return (
    <div ref={parentRef} className="h-full overflow-auto">
      <div style={{ height: rows.getTotalSize(), position: "relative" }}>
        {rows.getVirtualItems().map((vi) => {
          const th = threads[vi.index];
          const unread = !th.is_read;
          return (
            <div
              key={th.id}
              data-index={vi.index}
              ref={rows.measureElement}
              style={{ position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${vi.start}px)` }}
              onClick={() => onOpen(th)}
              className={`group flex cursor-pointer gap-2 border-b border-slate-800 px-3 py-2.5 transition-colors ${
                th.id === selectedId
                  ? "bg-blue-900/30 border-l-2 border-l-blue-500"
                  : selected.has(th.id)
                    ? "bg-blue-900/15"
                    : "hover:bg-slate-800/60 border-l-2 border-l-transparent"
              }`}
            >
              <input
                type="checkbox"
                checked={selected.has(th.id)}
                onClick={(e) => e.stopPropagation()}
                onChange={() => onToggleSelect(th.id)}
                className="mt-1 h-3.5 w-3.5 shrink-0 accent-blue-600 opacity-0 group-hover:opacity-100 checked:opacity-100"
              />
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onStar(th);
                }}
                className={`mt-0.5 shrink-0 text-sm ${th.is_starred ? "text-amber-400" : "text-slate-600 hover:text-slate-400"}`}
              >
                {th.is_starred ? "★" : "☆"}
              </button>
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className={`truncate text-sm ${unread ? "font-semibold text-slate-100" : "text-slate-300"}`}>
                    {th.sender ?? "—"}
                  </span>
                  <span className="shrink-0 text-[11px] text-slate-500">{relDate(th.last_message_at)}</span>
                </div>
                <div className="flex items-center gap-1.5">
                  <span className={`truncate text-xs ${unread ? "text-slate-200" : "text-slate-400"}`}>
                    {th.subject || "(без темы)"}
                  </span>
                  {th.message_count > 1 && (
                    <span className="text-[10px] text-slate-500">{th.message_count}</span>
                  )}
                  {th.has_attachments && <span className="text-[10px]">📎</span>}
                </div>
                {th.last_snippet && (
                  <p className="truncate text-[11px] text-slate-500">{th.last_snippet}</p>
                )}
                {th.labels.length > 0 && (
                  <div className="mt-0.5 flex flex-wrap gap-1">
                    {th.labels.map((l) => (
                      <span
                        key={l.id}
                        className="rounded-full px-1.5 text-[9px]"
                        style={{ background: (l.color ?? "#475569") + "33", color: l.color ?? "#cbd5e1" }}
                      >
                        {l.name}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
