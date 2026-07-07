"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import GenerationDetail from "@/components/studio/GenerationDetail";
import GenerationGallery from "@/components/studio/GenerationGallery";
import LoraTrainingPanel from "@/components/studio/LoraTrainingPanel";
import StudioComposer from "@/components/studio/StudioComposer";
import StudioQueuePanel from "@/components/studio/StudioQueuePanel";
import WorkflowPanel from "@/components/studio/WorkflowPanel";
import { getApiBaseUrl } from "@/lib/api-base";
import { gpuStatus } from "@/lib/lora-api";
import {
  clearFailedGenerations,
  deleteGeneration,
  Generation,
  getStudioJob,
  getGeneration,
  getStudioQueueStats,
  listGenerations,
  listStudioQueue,
  StudioJob,
  StudioJobKind,
  StudioQueueStats,
} from "@/lib/studio-api";

type Tab = "studio" | "queue" | "workflows" | "lora";

export default function StudioPage() {
  const t = useTranslations("studio");
  const [items, setItems] = useState<Generation[]>([]);
  const [jobs, setJobs] = useState<StudioJob[]>([]);
  const [queueStats, setQueueStats] = useState<StudioQueueStats | null>(null);
  const [queueStatusFilter, setQueueStatusFilter] = useState("");
  const [queueKindFilter, setQueueKindFilter] = useState<StudioJobKind | "">("");
  const [queueMineOnly, setQueueMineOnly] = useState(false);
  const [selected, setSelected] = useState<Generation | null>(null);
  const [tab, setTab] = useState<Tab>("studio");
  const [error, setError] = useState<string | null>(null);
  const [gpuBusy, setGpuBusy] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await listGenerations();
      const queue = await listStudioQueue({
        status: queueStatusFilter,
        kind: queueKindFilter,
        mine: queueMineOnly,
        limit: 120,
      });
      setItems(data);
      setJobs(queue);
      getStudioQueueStats()
        .then(setQueueStats)
        .catch(() => setQueueStats(null));
      setError(null);
      // Keep the open detail fresh.
      setSelected((cur) =>
        cur ? (data.find((g) => g.id === cur.id) ?? cur) : cur,
      );
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, [queueKindFilter, queueMineOnly, queueStatusFilter]);

  const onDelete = useCallback(
    async (g: Generation) => {
      try {
        await deleteGeneration(g.id);
        setSelected((cur) => (cur?.id === g.id ? null : cur));
        await load();
      } catch (e) {
        setError(String((e as Error).message || e));
      }
    },
    [load],
  );

  const onClearFailed = useCallback(async () => {
    try {
      await clearFailedGenerations();
      await load();
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, [load]);

  const failedCount = items.filter((g) => g.status === "failed").length;
  const activeJobs = jobs.some((j) =>
    ["queued", "waiting_resource", "running", "cancel_requested"].includes(j.status),
  );

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const url = `${getApiBaseUrl()}/api/studio/queue/events`;
    let source: EventSource | null = null;
    try {
      source = new EventSource(url, { withCredentials: true });
      source.addEventListener("queue", () => void load());
      source.addEventListener("error", () => {
        source?.close();
        source = null;
      });
    } catch {
      source = null;
    }
    return () => {
      source?.close();
    };
  }, [load]);

  // Poll while any generation is still running.
  useEffect(() => {
    const pending = items.some(
      (g) => g.status === "queued" || g.status === "running",
    );
    if ((pending || activeJobs) && !pollRef.current) {
      pollRef.current = setInterval(load, 2500);
    } else if (!pending && !activeJobs && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [items, activeJobs, load]);

  // GPU-lock banner: LoRA training makes local ComfyUI/Ollama unavailable
  // for every studio user, not just the run's owner.
  useEffect(() => {
    let alive = true;
    const check = () =>
      gpuStatus()
        .then((s) => alive && setGpuBusy(!!s.training_lock))
        .catch(() => undefined);
    void check();
    const id = setInterval(check, 30000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // Deep-link from a push notification: /studio?id=...
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const jobId = params.get("job");
    if (jobId) {
      getStudioJob(jobId)
        .then((job) => {
          if (job.generation_id) return getGeneration(job.generation_id);
          setTab("queue");
          return null;
        })
        .then((g) => g && setSelected(g))
        .catch(() => undefined);
      return;
    }
    const id = params.get("id");
    if (id) {
      getGeneration(id)
        .then((g) => setSelected(g))
        .catch(() => undefined);
    }
  }, []);

  return (
    <div className="flex flex-col h-full">
      <div className="px-4 sm:px-6 py-3 sm:py-4 border-b border-white/10 bg-zinc-900/60 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-lg sm:text-xl font-semibold text-white">
            {t("title")}
          </h1>
          <p className="text-xs text-zinc-500 hidden sm:block">
            {t("subtitle")}
          </p>
        </div>
        <div className="flex gap-1 overflow-x-auto -mx-4 px-4 sm:mx-0 sm:px-0">
          <button
            onClick={() => setTab("studio")}
            className={`shrink-0 px-3 py-1.5 rounded text-sm ${
              tab === "studio"
                ? "bg-white/10 text-white"
                : "text-zinc-400 hover:text-white"
            }`}
          >
            {t("tab_studio")}
          </button>
          <button
            onClick={() => setTab("queue")}
            className={`shrink-0 px-3 py-1.5 rounded text-sm ${
              tab === "queue"
                ? "bg-white/10 text-white"
                : "text-zinc-400 hover:text-white"
            }`}
          >
            {t("tab_queue")}
          </button>
          <button
            onClick={() => setTab("workflows")}
            className={`shrink-0 px-3 py-1.5 rounded text-sm ${
              tab === "workflows"
                ? "bg-white/10 text-white"
                : "text-zinc-400 hover:text-white"
            }`}
          >
            {t("tab_workflows")}
          </button>
          <button
            onClick={() => setTab("lora")}
            className={`shrink-0 px-3 py-1.5 rounded text-sm ${
              tab === "lora"
                ? "bg-white/10 text-white"
                : "text-zinc-400 hover:text-white"
            }`}
          >
            {t("tab_lora")}
          </button>
          <a
            href="/settings/comfyui"
            className="shrink-0 px-3 py-1.5 rounded text-sm text-zinc-400 hover:text-white"
          >
            {t("tab_settings")}
          </a>
        </div>
      </div>

      {gpuBusy && (
        <div className="mx-6 mt-3 text-xs text-amber-300 bg-amber-500/10 rounded p-2">
          GPU занят обучением LoRA — локальные ИИ-функции (генерация, правка,
          очистка) временно недоступны. Облачные маршруты работают.
        </div>
      )}

      {error && (
        <div className="mx-6 mt-3 text-xs text-red-400 bg-red-500/10 rounded p-2">
          {error}
        </div>
      )}

      {tab === "queue" ? (
        <div className="flex-1 overflow-y-auto p-4">
          <div className="mx-auto max-w-3xl">
            <StudioQueuePanel
              jobs={jobs}
              stats={queueStats}
              filterStatus={queueStatusFilter}
              filterKind={queueKindFilter}
              onlyMine={queueMineOnly}
              isOperator={!!queueStats}
              onFilterChange={(next) => {
                setQueueStatusFilter(next.status ?? "");
                setQueueKindFilter(next.kind ?? "");
                setQueueMineOnly(!!next.mine);
              }}
              onChanged={load}
              onOpenGeneration={(id) => {
                getGeneration(id)
                  .then((g) => setSelected(g))
                  .catch(() => undefined);
                }}
            />
          </div>
          {selected && (
            <div className="fixed inset-0 z-50 overflow-y-auto bg-zinc-950/95 p-4 xl:left-auto xl:w-[420px] xl:border-l xl:border-white/10">
              <GenerationDetail
                gen={selected}
                onChanged={load}
                onClose={() => setSelected(null)}
              />
            </div>
          )}
        </div>
      ) : tab === "workflows" ? (
        <div className="flex-1 min-h-0">
          <WorkflowPanel />
        </div>
      ) : tab === "lora" ? (
        <div className="flex-1 min-h-0 overflow-y-auto">
          <LoraTrainingPanel />
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto lg:overflow-hidden grid lg:grid-cols-[360px_1fr]">
          <div className="border-b lg:border-b-0 lg:border-r border-white/10 lg:overflow-y-auto p-4">
            <StudioComposer onSubmitted={load} />
          </div>
          <div className="lg:overflow-y-auto p-4 grid xl:grid-cols-[1fr_360px] gap-4">
            <div>
              <div className="mb-4">
                <StudioQueuePanel
                  jobs={jobs}
                  stats={queueStats}
                  filterStatus={queueStatusFilter}
                  filterKind={queueKindFilter}
                  onlyMine={queueMineOnly}
                  isOperator={!!queueStats}
                  onFilterChange={(next) => {
                    setQueueStatusFilter(next.status ?? "");
                    setQueueKindFilter(next.kind ?? "");
                    setQueueMineOnly(!!next.mine);
                  }}
                  onChanged={load}
                  onOpenGeneration={(id) => {
                    getGeneration(id)
                      .then((g) => setSelected(g))
                      .catch(() => undefined);
                  }}
                />
              </div>
              {failedCount > 0 && (
                <div className="flex justify-end mb-2">
                  <button
                    onClick={onClearFailed}
                    className="text-xs text-red-300 hover:text-red-200"
                  >
                    Очистить ошибочные ({failedCount})
                  </button>
                </div>
              )}
              <GenerationGallery
                items={items}
                selectedId={selected?.id ?? null}
                onSelect={setSelected}
                onDelete={onDelete}
              />
            </div>
            {selected && (
              // Mobile: a full-screen overlay so the actions (download/accept/
              // iterate) are reachable without scrolling past the whole gallery.
              // Desktop (xl): the inline side panel as before.
              <div className="fixed inset-0 z-50 overflow-y-auto bg-zinc-950/95 p-4 xl:static xl:z-auto xl:overflow-visible xl:rounded-lg xl:border xl:border-white/10 xl:bg-zinc-900/40 xl:h-fit">
                <GenerationDetail
                  gen={selected}
                  onChanged={load}
                  onClose={() => setSelected(null)}
                />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
