"use client";

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
}: {
  operations: EmgOperationNode[];
  guessedOperationIds: Set<string>;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (operations.length === 0) {
    return (
      <p className="p-3 text-xs text-zinc-500">Дерево построения пока пусто.</p>
    );
  }
  return (
    <ul className="divide-y divide-white/5">
      {operations.map((op, index) => {
        const active = op.id === selectedId;
        const guessed = guessedOperationIds.has(op.id);
        return (
          <li key={op.id}>
            <button
              type="button"
              onClick={() => onSelect(op.id)}
              className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs transition-colors ${
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
            </button>
          </li>
        );
      })}
    </ul>
  );
}
