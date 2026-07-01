"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import GenerationDetail from "@/components/studio/GenerationDetail";
import GenerationGallery from "@/components/studio/GenerationGallery";
import StudioComposer from "@/components/studio/StudioComposer";
import WorkflowPanel from "@/components/studio/WorkflowPanel";
import { Generation, getGeneration, listGenerations } from "@/lib/studio-api";

type Tab = "studio" | "workflows";

export default function StudioPage() {
  const t = useTranslations("studio");
  const [items, setItems] = useState<Generation[]>([]);
  const [selected, setSelected] = useState<Generation | null>(null);
  const [tab, setTab] = useState<Tab>("studio");
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await listGenerations();
      setItems(data);
      setError(null);
      // Keep the open detail fresh.
      setSelected((cur) =>
        cur ? (data.find((g) => g.id === cur.id) ?? cur) : cur,
      );
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Poll while any generation is still running.
  useEffect(() => {
    const pending = items.some(
      (g) => g.status === "queued" || g.status === "running",
    );
    if (pending && !pollRef.current) {
      pollRef.current = setInterval(load, 2500);
    } else if (!pending && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [items, load]);

  // Deep-link from a push notification: /studio?id=...
  useEffect(() => {
    const id = new URLSearchParams(window.location.search).get("id");
    if (id) {
      getGeneration(id)
        .then((g) => setSelected(g))
        .catch(() => undefined);
    }
  }, []);

  return (
    <div className="flex flex-col h-full">
      <div className="px-6 py-4 border-b border-white/10 bg-zinc-900/60 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-white">{t("title")}</h1>
          <p className="text-xs text-zinc-500">{t("subtitle")}</p>
        </div>
        <div className="flex gap-1">
          <button
            onClick={() => setTab("studio")}
            className={`px-3 py-1.5 rounded text-sm ${
              tab === "studio"
                ? "bg-white/10 text-white"
                : "text-zinc-400 hover:text-white"
            }`}
          >
            {t("tab_studio")}
          </button>
          <button
            onClick={() => setTab("workflows")}
            className={`px-3 py-1.5 rounded text-sm ${
              tab === "workflows"
                ? "bg-white/10 text-white"
                : "text-zinc-400 hover:text-white"
            }`}
          >
            {t("tab_workflows")}
          </button>
          <a
            href="/settings/comfyui"
            className="px-3 py-1.5 rounded text-sm text-zinc-400 hover:text-white"
          >
            {t("tab_settings")}
          </a>
        </div>
      </div>

      {error && (
        <div className="mx-6 mt-3 text-xs text-red-400 bg-red-500/10 rounded p-2">
          {error}
        </div>
      )}

      {tab === "workflows" ? (
        <div className="flex-1 overflow-y-auto p-6">
          <WorkflowPanel />
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto lg:overflow-hidden grid lg:grid-cols-[360px_1fr]">
          <div className="border-b lg:border-b-0 lg:border-r border-white/10 lg:overflow-y-auto p-4">
            <StudioComposer onSubmitted={load} />
          </div>
          <div className="lg:overflow-y-auto p-4 grid xl:grid-cols-[1fr_360px] gap-4">
            <GenerationGallery
              items={items}
              selectedId={selected?.id ?? null}
              onSelect={setSelected}
            />
            {selected && (
              <div className="rounded-lg border border-white/10 p-4 bg-zinc-900/40 h-fit">
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
