"use client";

import { useMemo, useState } from "react";

import { CadIr, IrPatchOp } from "@/lib/studio-api";

import { entityLabel } from "@/components/cad/geometry";

const PAGE_SIZE = 100;

/** Pending-review queue: filter by reason, page, confirm/delete one or a
 * whole page. Owns its filter/page state; edits go through the parent's
 * apply() so undo/history stay in one place. */
export default function ReviewPanel({
  ir,
  busy,
  onApply,
  onFocus,
  t,
}: {
  ir: CadIr;
  busy: boolean;
  onApply: (ops: IrPatchOp[]) => void;
  onFocus: (entityId: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const [reviewFilter, setReviewFilter] = useState<string>("all");
  const [reviewPage, setReviewPage] = useState(0);

  const pending = useMemo(
    () => (ir.review ?? []).filter((r) => !r.resolved),
    [ir],
  );
  const reviewReasons = useMemo(
    () => Array.from(new Set(pending.map((r) => r.reason))).sort(),
    [pending],
  );
  const filteredPending = useMemo(() => {
    const filtered =
      reviewFilter === "all"
        ? pending
        : pending.filter((r) => r.reason === reviewFilter);
    const entityById = new Map(
      (ir.entities ?? []).map((entity) => [entity.id, entity]),
    );
    return [...filtered].sort((a, b) => {
      const priority = (item: typeof a) => {
        const entity = entityById.get(item.entity_id);
        if (entity && (entity.type === "dimension" || entity.type === "text"))
          return 0;
        if (item.reason === "validation_error") return 1;
        if (item.reason === "unresolved_hypothesis") return 2;
        return 3;
      };
      return priority(a) - priority(b);
    });
  }, [pending, reviewFilter, ir]);

  if (pending.length === 0) return null;

  const pageCount = Math.max(1, Math.ceil(filteredPending.length / PAGE_SIZE));
  const safePage = Math.min(reviewPage, pageCount - 1);
  const visibleReview = filteredPending.slice(
    safePage * PAGE_SIZE,
    (safePage + 1) * PAGE_SIZE,
  );

  return (
    <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 space-y-1">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-amber-300">{t("vector.review_title")}</div>
        {reviewReasons.length > 1 && (
          <select
            value={reviewFilter}
            onChange={(e) => {
              setReviewFilter(e.target.value);
              setReviewPage(0);
            }}
            className="rounded bg-zinc-900 border border-white/10 px-1.5 py-0.5 text-[11px] text-zinc-300"
          >
            <option value="all">{t("vector.review_filter_all")}</option>
            {reviewReasons.map((reason) => (
              <option key={reason} value={reason}>
                {t(`vector.review_reason_${reason}`)}
              </option>
            ))}
          </select>
        )}
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={busy || visibleReview.length === 0}
            onClick={() =>
              onApply(
                visibleReview.map((item) => ({
                  op: "confirm" as const,
                  entity_id: item.entity_id,
                })),
              )
            }
            className="rounded bg-emerald-600/70 px-2 py-0.5 text-[11px] text-white hover:bg-emerald-500 disabled:opacity-40"
          >
            {t("vector.review_confirm_page")}
          </button>
          <button
            type="button"
            disabled={busy || visibleReview.length === 0}
            onClick={() => {
              if (
                window.confirm(
                  t("vector.review_delete_page_confirm", {
                    n: visibleReview.length,
                  }),
                )
              ) {
                onApply(
                  visibleReview.map((item) => ({
                    op: "delete" as const,
                    entity_id: item.entity_id,
                  })),
                );
              }
            }}
            className="rounded bg-red-600/60 px-2 py-0.5 text-[11px] text-white hover:bg-red-500 disabled:opacity-40"
          >
            {t("vector.review_delete_page")}
          </button>
        </div>
      </div>
      <div className="max-h-48 overflow-y-auto space-y-1">
        {visibleReview.map((r) => {
          const e = ir.entities.find((x) => x.id === r.entity_id);
          if (!e) return null;
          return (
            <div
              key={r.entity_id}
              className="flex items-center gap-2 text-[11px]"
            >
              <button
                onClick={() => onFocus(r.entity_id)}
                className="flex flex-1 items-center gap-1.5 text-left text-zinc-300 hover:text-white truncate"
              >
                <span className="truncate">
                  {entityLabel(e, t)} — {Math.round(e.confidence * 100)}%
                </span>
                <span className="shrink-0 text-zinc-500">
                  {t(`vector.review_reason_${r.reason}`)}
                </span>
              </button>
              <button
                disabled={busy}
                onClick={() =>
                  onApply([{ op: "confirm", entity_id: r.entity_id }])
                }
                className="text-emerald-400 hover:text-emerald-300 disabled:opacity-40"
              >
                ✓
              </button>
              <button
                disabled={busy}
                onClick={() =>
                  onApply([{ op: "delete", entity_id: r.entity_id }])
                }
                className="text-red-400 hover:text-red-300 disabled:opacity-40"
              >
                ✕
              </button>
            </div>
          );
        })}
      </div>
      {pageCount > 1 && (
        <div className="flex items-center justify-between text-[11px] text-zinc-400">
          <button
            type="button"
            disabled={safePage === 0}
            onClick={() => setReviewPage((page) => Math.max(0, page - 1))}
            className="rounded bg-white/5 px-2 py-0.5 hover:bg-white/10 disabled:opacity-30"
          >
            {t("vector.review_prev")}
          </button>
          <span>
            {t("vector.review_page", {
              page: safePage + 1,
              pages: pageCount,
              n: filteredPending.length,
            })}
          </span>
          <button
            type="button"
            disabled={safePage >= pageCount - 1}
            onClick={() =>
              setReviewPage((page) => Math.min(pageCount - 1, page + 1))
            }
            className="rounded bg-white/5 px-2 py-0.5 hover:bg-white/10 disabled:opacity-30"
          >
            {t("vector.review_next")}
          </button>
        </div>
      )}
    </div>
  );
}
