"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import {
  bulkCancelStudioQueue,
  cancelStudioJob,
  retryStudioJob,
  setStudioJobPriority,
  setStudioQueueControl,
  StudioJob,
  StudioJobKind,
  StudioQueueStats,
} from "@/lib/studio-api";

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
  stats?: StudioQueueStats | null;
  filterStatus?: string;
  filterKind?: StudioJobKind | "";
  onlyMine?: boolean;
  isOperator?: boolean;
  onFilterChange?: (next: { status?: string; kind?: StudioJobKind | ""; mine?: boolean }) => void;
  onChanged: () => void;
  onOpenGeneration?: (id: string) => void;
}

function fmtDuration(seconds: number | null | undefined): string {
  if (!seconds && seconds !== 0) return "";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${Math.round(seconds / 3600)}h`;
}

export default function StudioQueuePanel({
  jobs,
  stats,
  filterStatus = "",
  filterKind = "",
  onlyMine = false,
  isOperator = false,
  onFilterChange,
  onChanged,
  onOpenGeneration,
}: Props) {
  const t = useTranslations("studio");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  const [priorityDraft, setPriorityDraft] = useState<Record<string, number>>({});
  const active = jobs.filter((j) =>
    ["queued", "waiting_resource", "running", "cancel_requested"].includes(j.status),
  );
  const recent = jobs.filter((j) => !active.includes(j)).slice(0, 6);
  const shown = [...active, ...recent].slice(0, 40);

  async function cancel(job: StudioJob) {
    setBusyId(job.id);
    try {
      await cancelStudioJob(job.id);
      onChanged();
    } finally {
      setBusyId(null);
    }
  }

  async function retry(job: StudioJob) {
    setBusyId(job.id);
    try {
      await retryStudioJob(job.id);
      onChanged();
    } finally {
      setBusyId(null);
    }
  }

  async function savePriority(job: StudioJob) {
    setBusyId(job.id);
    try {
      await setStudioJobPriority(job.id, priorityDraft[job.id] ?? job.priority);
      onChanged();
    } finally {
      setBusyId(null);
    }
  }

  async function patchControl(next: { paused?: boolean; drain?: boolean }) {
    setControlBusy(true);
    try {
      await setStudioQueueControl({ ...next, reason: next.paused || next.drain ? "Операторское управление очередью" : null });
      onChanged();
    } finally {
      setControlBusy(false);
    }
  }

  async function bulkCancel() {
    setControlBusy(true);
    try {
      await bulkCancelStudioQueue({});
      onChanged();
    } finally {
      setControlBusy(false);
    }
  }

  return (
    <div className="rounded border border-white/10 bg-zinc-900/40">
      <div className="flex items-center justify-between border-b border-white/10 px-3 py-2">
        <div className="text-sm font-medium text-zinc-200">{t("queue.title")}</div>
        <div className="text-[11px] text-zinc-500">
          {active.length ? t("queue.active_count", { count: active.length }) : t("queue.no_active")}
        </div>
      </div>
      <div className="grid gap-2 border-b border-white/10 p-3 text-[11px] text-zinc-400 sm:grid-cols-3">
        <div className="rounded border border-white/10 bg-black/20 p-2">
          <div className="text-zinc-500">{t("queue.metrics.active")}</div>
          <div className="mt-1 text-base font-medium text-zinc-100">
            {stats ? `${stats.active}/${stats.limits.global_active}` : active.length}
          </div>
        </div>
        <div className="rounded border border-white/10 bg-black/20 p-2">
          <div className="text-zinc-500">{t("queue.metrics.wait")}</div>
          <div className="mt-1 text-base font-medium text-zinc-100">
            {fmtDuration(stats?.avg_wait_seconds_24h) || "-"}
          </div>
        </div>
        <div className="rounded border border-white/10 bg-black/20 p-2">
          <div className="text-zinc-500">{t("queue.metrics.runtime")}</div>
          <div className="mt-1 text-base font-medium text-zinc-100">
            {fmtDuration(stats?.avg_runtime_seconds_24h) || "-"}
          </div>
        </div>
      </div>
      <div className="flex flex-col gap-2 border-b border-white/10 p-3 sm:flex-row sm:items-center">
        <select
          value={filterStatus}
          onChange={(e) => onFilterChange?.({ status: e.target.value, kind: filterKind, mine: onlyMine })}
          className="min-h-9 rounded border border-white/10 bg-zinc-950 px-2 text-xs text-zinc-200"
        >
          <option value="">{t("queue.filters.all")}</option>
          <option value="queued,waiting_resource,running,cancel_requested">{t("queue.filters.active")}</option>
          <option value="failed">{t("queue.filters.failed")}</option>
          <option value="done">{t("queue.filters.done")}</option>
          <option value="cancelled">{t("queue.filters.cancelled")}</option>
        </select>
        <select
          value={filterKind}
          onChange={(e) => onFilterChange?.({ status: filterStatus, kind: e.target.value as StudioJobKind | "", mine: onlyMine })}
          className="min-h-9 rounded border border-white/10 bg-zinc-950 px-2 text-xs text-zinc-200"
        >
          <option value="">{t("queue.filters.any_kind")}</option>
          <option value="image_generation">{t("queue.kind.image_generation")}</option>
          <option value="lora_training">{t("queue.kind.lora_training")}</option>
        </select>
        <label className="flex min-h-9 items-center gap-2 rounded border border-white/10 px-2 text-xs text-zinc-300">
          <input
            type="checkbox"
            checked={onlyMine}
            onChange={(e) => onFilterChange?.({ status: filterStatus, kind: filterKind, mine: e.target.checked })}
          />
          {t("queue.filters.mine")}
        </label>
      </div>
      {isOperator && stats && (
        <div className="flex flex-wrap gap-2 border-b border-white/10 p-3">
          <button
            type="button"
            disabled={controlBusy}
            onClick={() => void patchControl({ paused: !stats.control.paused })}
            className="rounded bg-white/10 px-2 py-1.5 text-xs text-zinc-100 hover:bg-white/20 disabled:opacity-50"
          >
            {stats.control.paused ? t("queue.controls.resume") : t("queue.controls.pause")}
          </button>
          <button
            type="button"
            disabled={controlBusy}
            onClick={() => void patchControl({ drain: !stats.control.drain })}
            className="rounded bg-white/10 px-2 py-1.5 text-xs text-zinc-100 hover:bg-white/20 disabled:opacity-50"
          >
            {stats.control.drain ? t("queue.controls.stop_drain") : t("queue.controls.drain")}
          </button>
          <button
            type="button"
            disabled={controlBusy}
            onClick={() => void bulkCancel()}
            className="rounded bg-red-500/15 px-2 py-1.5 text-xs text-red-200 hover:bg-red-500/25 disabled:opacity-50"
          >
            {t("queue.controls.cancel_pending")}
          </button>
          {(stats.control.paused || stats.control.drain) && (
            <div className="basis-full text-[11px] text-amber-300">
              {stats.control.reason || t("queue.controls.restricted")}
            </div>
          )}
        </div>
      )}
      {shown.length === 0 && (
        <div className="p-3 text-xs text-zinc-500">{t("queue.empty")}</div>
      )}
      <div className="divide-y divide-white/10">
        {shown.map((job) => {
          const pct = progressPct(job);
          const clickable = job.generation_id && onOpenGeneration;
          const eta = fmtDuration(job.eta_seconds);
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
                    {eta && <span className="text-[11px] text-zinc-500">{t("queue.eta", { eta })}</span>}
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
                <div className="flex shrink-0 flex-col items-end gap-1">
                  {isOperator && !["done", "failed", "cancelled"].includes(job.status) && (
                    <div className="flex items-center gap-1">
                      <input
                        type="number"
                        min={-100}
                        max={100}
                        value={priorityDraft[job.id] ?? job.priority}
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => setPriorityDraft((cur) => ({ ...cur, [job.id]: Number(e.target.value) }))}
                        className="h-7 w-14 rounded border border-white/10 bg-zinc-950 px-1 text-right text-[11px] text-zinc-200"
                      />
                      <button
                        type="button"
                        disabled={busyId === job.id}
                        onClick={(e) => {
                          e.stopPropagation();
                          void savePriority(job);
                        }}
                        className="rounded bg-white/10 px-2 py-1 text-[11px] text-zinc-200 hover:bg-white/20 disabled:opacity-50"
                      >
                        {t("queue.priority")}
                      </button>
                    </div>
                  )}
                  {job.can_retry && isOperator && (
                    <button
                      type="button"
                      disabled={busyId === job.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        void retry(job);
                      }}
                      className="rounded bg-sky-500/15 px-2 py-1 text-[11px] text-sky-200 hover:bg-sky-500/25 disabled:opacity-50"
                    >
                      {t("queue.retry")}
                    </button>
                  )}
                  {job.can_cancel && (
                    <button
                      type="button"
                      disabled={busyId === job.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        void cancel(job);
                      }}
                      className="rounded bg-white/10 px-2 py-1 text-[11px] text-zinc-200 hover:bg-white/20 disabled:opacity-50"
                    >
                      {busyId === job.id ? t("queue.cancel_busy") : t("queue.cancel")}
                    </button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
