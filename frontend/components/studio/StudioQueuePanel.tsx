"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { StudioJob, cancelStudioJob } from "@/lib/studio-api";

const STATUS_COLOR: Record<string, string> = {
  queued: "text-amber-400",
  waiting_resource: "text-amber-300",
  running: "text-sky-400",
  cancel_requested: "text-orange-300",
  cancelled: "text-zinc-500",
  done: "text-emerald-400",
  failed: "text-red-400",
};

function progressPct(job: StudioJob): number | null {
  const progress = job.progress as Record<string, unknown> | null;
  if (!progress) return null;
  if (typeof progress.pct === "number") return Math.max(0, Math.min(100, progress.pct));
  if (typeof progress.step === "number" && typeof progress.total === "number" && progress.total > 0) {
    return Math.round((progress.step / progress.total) * 100);
  }
  return null;
}

interface Props {
  jobs: StudioJob[];
  onChanged: () => void;
  onOpenGeneration?: (id: string) => void;
}

export default function StudioQueuePanel({ jobs, onChanged, onOpenGeneration }: Props) {
  const t = useTranslations("studio");
  const [busyId, setBusyId] = useState<string | null>(null);
  const active = jobs.filter((j) =>
    ["queued", "waiting_resource", "running", "cancel_requested"].includes(j.status),
  );
  const recent = jobs.filter((j) => !active.includes(j)).slice(0, 6);
  const shown = [...active, ...recent].slice(0, 12);

  async function cancel(job: StudioJob) {
    setBusyId(job.id);
    try {
      await cancelStudioJob(job.id);
      onChanged();
    } finally {
      setBusyId(null);
    }
  }

  if (shown.length === 0) {
    return (
      <div className="rounded border border-white/10 bg-zinc-900/40 p-3 text-xs text-zinc-500">
        {t("queue.empty")}
      </div>
    );
  }

  return (
    <div className="rounded border border-white/10 bg-zinc-900/40">
      <div className="flex items-center justify-between border-b border-white/10 px-3 py-2">
        <div className="text-sm font-medium text-zinc-200">{t("queue.title")}</div>
        <div className="text-[11px] text-zinc-500">
          {active.length ? t("queue.active_count", { count: active.length }) : t("queue.no_active")}
        </div>
      </div>
      <div className="divide-y divide-white/10">
        {shown.map((job) => {
          const pct = progressPct(job);
          const clickable = job.generation_id && onOpenGeneration;
          return (
            <div
              key={job.id}
              className={`p-3 ${clickable ? "cursor-pointer hover:bg-white/[0.03]" : ""}`}
              onClick={() => clickable && onOpenGeneration(job.generation_id!)}
              role={clickable ? "button" : undefined}
              tabIndex={clickable ? 0 : undefined}
              onKeyDown={(e) => {
                if (clickable && e.key === "Enter") onOpenGeneration(job.generation_id!);
              }}
            >
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className={`text-xs ${STATUS_COLOR[job.status] ?? "text-zinc-400"}`}>
                      {t(`queue.status.${job.status}`)}
                    </span>
                    {job.position && (
                      <span className="text-[11px] text-zinc-500">
                        {t("queue.position", { position: job.position })}
                      </span>
                    )}
                    <span className="text-[11px] text-zinc-600">{job.resource}</span>
                  </div>
                  <div className="mt-1 truncate text-xs text-zinc-300">
                    {job.title || t(`queue.kind.${job.kind}`)}
                  </div>
                  {pct !== null && (
                    <div className="mt-2 h-1.5 overflow-hidden rounded bg-zinc-800">
                      <div className="h-full bg-sky-500 transition-all" style={{ width: `${pct}%` }} />
                    </div>
                  )}
                  {job.error && job.status !== "done" && (
                    <div className="mt-1 line-clamp-2 text-[11px] text-red-300">{job.error}</div>
                  )}
                </div>
                {job.can_cancel && (
                  <button
                    type="button"
                    disabled={busyId === job.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      void cancel(job);
                    }}
                    className="shrink-0 rounded bg-white/10 px-2 py-1 text-[11px] text-zinc-200 hover:bg-white/20 disabled:opacity-50"
                  >
                    {busyId === job.id ? t("queue.cancel_busy") : t("queue.cancel")}
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
