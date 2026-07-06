"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import {
  COMFYUI_PROXY_URL,
  Workflow,
  deleteWorkflow,
  duplicateWorkflow,
  listWorkflows,
  patchWorkflow,
  pushWorkflowToComfyUI,
} from "@/lib/studio-api";

export default function WorkflowPanel() {
  const t = useTranslations("studio.workflow");
  const [items, setItems] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Workflow | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);

  async function load() {
    setLoading(true);
    setErr(null);
    try {
      setItems(await listWorkflows());
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const byCategory = items.reduce<Record<string, Workflow[]>>((acc, w) => {
    (acc[w.category] ??= []).push(w);
    return acc;
  }, {});

  /** Our stored templates carry a placeholder filename ("input.png") in
   * LoadImage nodes — it's never actually read at generation time (the real
   * upload always overwrites it in build_workflow(), see comfyui_client.py),
   * it only exists so the graph has a valid node shape. Forcing that
   * placeholder onto the widget when we load the template for viewing/
   * editing makes ComfyUI show a broken "file not found" thumbnail on any
   * server that doesn't happen to have a file with that exact name. Leaving
   * the input unset instead lets ComfyUI fall back to its own combo-widget
   * default (the first file it actually has) — a real, loadable preview. */
  function stripPlaceholderImageInputs(graph: Record<string, unknown>) {
    const cloned = JSON.parse(JSON.stringify(graph)) as Record<
      string,
      { class_type?: string; inputs?: Record<string, unknown> }
    >;
    for (const node of Object.values(cloned)) {
      if (node?.class_type === "LoadImage" && node.inputs) {
        delete node.inputs.image;
      }
    }
    return cloned;
  }

  /** Loads the graph straight onto the embedded ComfyUI canvas — no manual
   * navigation in ComfyUI's own UI needed. Bridged via a script our proxy
   * injects into ComfyUI's HTML (see backend/app/api/comfyui_proxy.py),
   * which calls the same `app.loadApiJson(...)` ComfyUI itself uses when a
   * user drags an API-format JSON file onto its canvas. */
  function openOnCanvas(w: Workflow) {
    const win = iframeRef.current?.contentWindow;
    if (!win) return;
    win.postMessage(
      {
        type: "ai-docs-load-workflow",
        graph: stripPlaceholderImageInputs(w.graph),
        name: w.title,
      },
      window.location.origin,
    );
  }

  function selectWorkflow(w: Workflow) {
    setSaveMsg(null);
    setSelected((cur) => (cur?.id === w.id ? null : w));
    openOnCanvas(w);
  }

  async function handleSaveToComfyUI() {
    if (!selected) return;
    setSaving(true);
    setSaveMsg(null);
    try {
      const r = await pushWorkflowToComfyUI(selected.id);
      setSaveMsg(
        t("saved_hint", {
          filename: r.filename.split("/").pop() ?? r.filename,
        }),
      );
    } catch (e) {
      setSaveMsg(String((e as Error).message || e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex flex-col lg:flex-row h-full min-h-0">
      {/* Main area: the live ComfyUI node-graph editor fills all remaining
          space — this is where nodes are actually visible/editable. The node
          graph is a desktop tool, so on phones we hide the iframe and keep the
          list/actions usable instead. */}
      <div className="relative flex-1 min-h-[30vh] min-w-0 bg-black/20">
        <iframe
          ref={iframeRef}
          src={COMFYUI_PROXY_URL}
          className="absolute inset-0 hidden h-full w-full border-0 lg:block"
          title="ComfyUI"
        />
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-6 text-center lg:hidden">
          <svg
            className="h-8 w-8 text-zinc-600"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.6}
              d="M4 6h16M4 12h16M4 18h10"
            />
          </svg>
          <p className="text-xs text-zinc-500">
            Визуальный редактор узлов доступен на компьютере. На телефоне можно
            просматривать, включать и удалять воркфлоу из списка ниже.
          </p>
        </div>

        {/* Bottom drawer: selecting a workflow on the right opens its
            compact details/actions here, sliding up from the bottom edge —
            it never covers the whole canvas, so the node graph stays visible. */}
        <div
          className={`absolute inset-x-0 bottom-0 border-t border-white/10 bg-zinc-950/95 backdrop-blur shadow-[0_-8px_24px_rgba(0,0,0,0.4)] transition-transform duration-200 ease-out ${
            selected ? "translate-y-0" : "translate-y-full pointer-events-none"
          }`}
        >
          {selected && (
            <div className="p-3 space-y-2 max-h-[38vh] overflow-y-auto">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="text-sm font-medium text-zinc-100 truncate">
                    {selected.title}
                  </h3>
                  <span className="text-[11px] text-zinc-500">
                    {selected.operation}
                  </span>
                </div>
                <button
                  onClick={() => setSelected(null)}
                  className="shrink-0 text-zinc-500 hover:text-zinc-200 text-xs px-1"
                  aria-label={t("close")}
                >
                  ✕
                </button>
              </div>
              {selected.description && (
                <p className="text-xs text-zinc-400">{selected.description}</p>
              )}
              <div className="flex flex-wrap gap-2">
                <button
                  onClick={() => openOnCanvas(selected)}
                  className="px-2.5 py-1 rounded bg-sky-500/20 hover:bg-sky-500/30 text-sky-200 text-xs"
                >
                  {t("open_in_comfyui")}
                </button>
                <button
                  onClick={handleSaveToComfyUI}
                  disabled={saving}
                  className="px-2.5 py-1 rounded bg-white/10 hover:bg-white/20 text-xs disabled:opacity-50"
                >
                  {saving ? t("saving") : t("save_to_comfyui")}
                </button>
                <button
                  onClick={async () => {
                    await duplicateWorkflow(selected.id);
                    await load();
                  }}
                  className="px-2.5 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
                >
                  {t("duplicate")}
                </button>
                <button
                  onClick={async () => {
                    const upd = await patchWorkflow(selected.id, {
                      enabled: !selected.enabled,
                    });
                    setSelected(upd);
                    await load();
                  }}
                  className="px-2.5 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
                >
                  {selected.enabled ? t("disable") : t("enable")}
                </button>
                {!selected.is_builtin && (
                  <button
                    onClick={async () => {
                      await deleteWorkflow(selected.id);
                      setSelected(null);
                      await load();
                    }}
                    className="px-2.5 py-1 rounded bg-red-500/15 hover:bg-red-500/25 text-red-300 text-xs"
                  >
                    {t("delete")}
                  </button>
                )}
              </div>
              {saveMsg && (
                <p className="text-[11px] text-emerald-400">{saveMsg}</p>
              )}
              {selected.is_builtin && (
                <p className="text-[11px] text-amber-400/80">
                  {t("builtin_readonly")}
                </p>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Right sidebar: compact workflow list, grouped by category. On phones
          it drops below the (collapsed) editor as a short scrollable list. */}
      <div className="w-full lg:w-60 shrink-0 border-t lg:border-t-0 lg:border-l border-white/10 max-h-56 lg:max-h-none overflow-y-auto p-2 space-y-3">
        {loading && (
          <div className="text-xs text-zinc-500 px-1">{t("loading")}</div>
        )}
        {err && <div className="text-xs text-red-400 px-1">{err}</div>}
        {Object.entries(byCategory).map(([cat, ws]) => (
          <div key={cat}>
            <h4 className="text-[10px] uppercase tracking-wide text-zinc-500 mb-1 px-1">
              {t.has(`category.${cat}`) ? t(`category.${cat}`) : cat}
            </h4>
            <div className="space-y-0.5">
              {ws.map((w) => (
                <button
                  key={w.id}
                  onClick={() => selectWorkflow(w)}
                  title={w.title}
                  className={`w-full text-left px-2 py-1 rounded text-xs truncate ${
                    selected?.id === w.id
                      ? "bg-sky-500/15 text-sky-200"
                      : "text-zinc-300 hover:bg-white/5"
                  } ${w.enabled ? "" : "opacity-50"}`}
                >
                  {w.title}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
