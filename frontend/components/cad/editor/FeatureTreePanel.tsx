"use client";

import { useRef, useState } from "react";

import type { EmgOperationNode } from "@/lib/emg-tree";
import { kindLabel } from "@/lib/emg-tree";

const KIND_ICON: Record<string, string> = {
  extrude: "▭",
  revolve: "◎",
  loft: "▱",
  sweep: "〰",
  shell: "▢",
  thread: "⌀",
  hole: "◯",
  boss: "⬤",
  pocket: "▣",
  fillet: "◜",
  chamfer: "◤",
  groove: "▬",
  keyway: "▤",
  rib: "▮",
  section_outer: "◐",
  section_bore: "◑",
  cross_hole: "◍",
  axial_hole_pattern: "⁙",
  circular_hole_pattern: "⁘",
  hole_pattern: "⁛",
  slot: "▭",
};

/** Ф6: inline "delete this operation?" — no modal library in this
 * codebase, matches the confirm-in-place pattern already used elsewhere
 * (e.g. ConstraintsPanel.tsx's own inline affordances). Text differs by
 * whether the operation has a source Feature (read off the drawing) or
 * not (added directly in the editor, featureIds is empty) — deleting a
 * READ operation means asserting "the drawing doesn't actually have
 * this", a stronger claim than removing something a human added by hand. */
function DeleteButton({
  op,
  busy,
  onConfirm,
}: {
  op: EmgOperationNode;
  busy: boolean;
  onConfirm: (reason: string) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");
  if (confirming) {
    return (
      <span
        className="flex shrink-0 items-center gap-1 text-[10px]"
        onClick={(event) => event.stopPropagation()}
      >
        <input
          type="text"
          value={reason}
          autoFocus
          aria-label="Причина удаления операции"
          placeholder="Причина удаления"
          onChange={(event) => setReason(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setReason("");
              setConfirming(false);
            }
            if (event.key === "Enter" && reason.trim()) {
              setConfirming(false);
              onConfirm(reason.trim());
              setReason("");
            }
          }}
          className="w-32 rounded border border-white/10 bg-zinc-950 px-1.5 py-0.5 text-zinc-200 outline-none focus:border-sky-500/60"
        />
        <button
          type="button"
          disabled={busy || !reason.trim()}
          onClick={() => {
            setConfirming(false);
            onConfirm(reason.trim());
            setReason("");
          }}
          className="rounded bg-red-500/20 px-1.5 py-0.5 text-red-300 hover:bg-red-500/30 disabled:opacity-40"
        >
          Да
        </button>
        <button
          type="button"
          onClick={() => {
            setReason("");
            setConfirming(false);
          }}
          className="rounded px-1.5 py-0.5 text-zinc-400 hover:bg-white/10"
        >
          Отмена
        </button>
      </span>
    );
  }
  return (
    <button
      type="button"
      title={
        op.featureIds.length > 0
          ? "Эта операция прочитана с чертежа — удаление означает «на чертеже её не должно быть»"
          : "Удалить добавленную операцию"
      }
      onClick={(event) => {
        event.stopPropagation();
        setConfirming(true);
      }}
      className="shrink-0 rounded px-1 text-zinc-600 opacity-0 hover:bg-red-500/15 hover:text-red-300 group-hover:opacity-100"
    >
      ✕
    </button>
  );
}

/** The build's own operation sequence, one row per BuildOperation — what
 * the part is actually made of, in the order the kernel applies it. A
 * guessed param anywhere in an operation (or one of the Feature nodes it
 * realizes) marks the row amber, so a person scanning the list sees WHERE
 * to look before they open anything. */
export default function FeatureTreePanel({
  operations,
  guessedOperationIds,
  selectedId,
  onSelect,
  onDelete,
  deleteBusyId,
}: {
  operations: EmgOperationNode[];
  guessedOperationIds: Set<string>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onDelete?: (id: string, reason: string) => void;
  deleteBusyId?: string | null;
}) {
  const [filter, setFilter] = useState<"all" | "review" | "without_guess">(
    "all",
  );
  const listRef = useRef<HTMLUListElement | null>(null);
  const visibleOperations = operations.filter((operation) => {
    const guessed = guessedOperationIds.has(operation.id);
    if (filter === "review") return guessed;
    if (filter === "without_guess") return !guessed;
    return true;
  });

  if (operations.length === 0) {
    return (
      <p className="p-3 text-xs text-zinc-500">Дерево построения пока пусто.</p>
    );
  }
  return (
    <>
      <div
        className="flex flex-wrap gap-1 border-b border-white/10 p-2"
        aria-label="Фильтр дерева построения"
      >
        {([
          ["all", `Все ${operations.length}`],
          ["review", `Проверить ${guessedOperationIds.size}`],
          [
            "without_guess",
            `Без предположений ${operations.length - guessedOperationIds.size}`,
          ],
        ] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={filter === value}
            onClick={() => setFilter(value)}
            className={`rounded px-2 py-1 text-[10px] transition-colors ${
              filter === value
                ? "bg-sky-500/20 text-sky-200"
                : "bg-white/5 text-zinc-400 hover:bg-white/10 hover:text-zinc-200"
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {visibleOperations.length === 0 ? (
        <p className="p-3 text-xs text-zinc-500">
          В этой категории операций нет.
        </p>
      ) : (
        <ul ref={listRef} className="divide-y divide-white/5">
          {visibleOperations.map((op, visibleIndex) => {
            const index = operations.findIndex((item) => item.id === op.id);
            const active = op.id === selectedId;
            const guessed = guessedOperationIds.has(op.id);
            return (
              <li key={op.id} className="group relative">
                {/* A <div role="button">, not a <button> — DeleteButton below
                    renders its own real <button>s, and nesting <button> inside
                    <button> is invalid HTML (the browser would silently close
                    the outer one early, breaking the row's own click target). */}
                <div
                  role="button"
                  tabIndex={0}
                  data-operation-index={visibleIndex}
                  onClick={() => onSelect(op.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(op.id);
                      return;
                    }
                    const nextIndex =
                      event.key === "ArrowDown"
                        ? Math.min(
                            visibleIndex + 1,
                            visibleOperations.length - 1,
                          )
                        : event.key === "ArrowUp"
                          ? Math.max(visibleIndex - 1, 0)
                          : event.key === "Home"
                            ? 0
                            : event.key === "End"
                              ? visibleOperations.length - 1
                              : null;
                    if (nextIndex === null) return;
                    event.preventDefault();
                    onSelect(visibleOperations[nextIndex].id);
                    listRef.current
                      ?.querySelector<HTMLElement>(
                        `[data-operation-index="${nextIndex}"]`,
                      )
                      ?.focus();
                  }}
                  className={`flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs transition-colors ${
                    active
                      ? "bg-sky-500/15 text-sky-100"
                      : "text-zinc-300 hover:bg-white/5"
                  }`}
                >
                  <span className="w-4 shrink-0 text-center text-zinc-500">
                    {index + 1}
                  </span>
                  <span className="w-5 shrink-0 text-center text-sm">
                    {KIND_ICON[op.kind] ?? "•"}
                  </span>
                  <span className="min-w-0 flex-1 truncate">
                    {kindLabel(op.kind)}
                  </span>
                  {guessed && (
                    <span
                      title="Есть предположенные значения — требует проверки"
                      className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400"
                    />
                  )}
                  {onDelete && (
                    <DeleteButton
                      op={op}
                      busy={deleteBusyId === op.id}
                      onConfirm={(reason) => onDelete(op.id, reason)}
                    />
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}
