"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

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
  const [pushMsg, setPushMsg] = useState<string | null>(null);
  const [pushing, setPushing] = useState(false);
  const [iframeNonce, setIframeNonce] = useState(0);

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

  function selectWorkflow(w: Workflow) {
    setPushMsg(null);
    setSelected((cur) => (cur?.id === w.id ? null : w));
  }

  async function handlePush() {
    if (!selected) return;
    setPushing(true);
    setPushMsg(null);
    try {
      const r = await pushWorkflowToComfyUI(selected.id);
      setPushMsg(
        t("pushed_hint", {
          filename: r.filename.split("/").pop() ?? r.filename,
        }),
      );
      // ComfyUI's own userdata cache is refreshed on reload — its Workflow
      // browser then shows the file we just saved.
      setIframeNonce((n) => n + 1);
    } catch (e) {
      setPushMsg(String((e as Error).message || e));
    } finally {
      setPushing(false);
    }
  }

  return (
    <div className="flex h-full min-h-0">
      {/* Main area: the live ComfyUI node-graph editor fills all remaining
          space — this is where nodes are actually visible/editable. */}
      <div className="relative flex-1 min-w-0 bg-black/20">
        <iframe
          key={iframeNonce}
          src={COMFYUI_PROXY_URL}
          className="absolute inset-0 h-full w-full border-0"
          title="ComfyUI"
        />

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
                  onClick={handlePush}
                  disabled={pushing}
                  className="px-2.5 py-1 rounded bg-sky-500/20 hover:bg-sky-500/30 text-sky-200 text-xs disabled:opacity-50"
                >
                  {pushing ? t("opening") : t("open_in_comfyui")}
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
              {pushMsg && (
                <p className="text-[11px] text-emerald-400">{pushMsg}</p>
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

      {/* Right sidebar: compact workflow list, grouped by category. */}
      <div className="w-60 shrink-0 border-l border-white/10 overflow-y-auto p-2 space-y-3">
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
