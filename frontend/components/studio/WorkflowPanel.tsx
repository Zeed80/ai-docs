"use client";

import { useTranslations } from "next-intl";
import { useEffect, useMemo, useRef, useState } from "react";

import {
  COMFYUI_PROXY_URL,
  Workflow,
  createWorkflow,
  deleteWorkflow,
  duplicateWorkflow,
  listWorkflows,
  patchWorkflow,
  pushWorkflowToComfyUI,
} from "@/lib/studio-api";

// Logical keys the engine's inject_map understands (see comfyui_client.build_workflow).
const INJECT_KEYS = ["prompt", "negative", "image", "mask", "seed"] as const;
type InjectKey = (typeof INJECT_KEYS)[number];
type ComfyNode = { class_type?: string; inputs?: Record<string, unknown> };

/** Best-effort prefill of the node/input for each logical key from a pasted
 * ComfyUI API graph — the user can correct any of it before saving. */
function autodetectMap(
  graph: Record<string, ComfyNode>,
): Record<InjectKey, { node: string; input: string }> {
  const empty = { node: "", input: "" };
  const map: Record<InjectKey, { node: string; input: string }> = {
    prompt: { ...empty },
    negative: { ...empty },
    image: { ...empty },
    mask: { ...empty },
    seed: { ...empty },
  };
  const entries = Object.entries(graph);
  const clip = entries.filter(([, n]) =>
    (n.class_type || "").includes("CLIPTextEncode"),
  );
  if (clip[0]) map.prompt = { node: clip[0][0], input: "text" };
  if (clip[1]) map.negative = { node: clip[1][0], input: "text" };
  const loadImg = entries.find(([, n]) => n.class_type === "LoadImage");
  if (loadImg) map.image = { node: loadImg[0], input: "image" };
  const maskNode = entries.find(([, n]) =>
    (n.class_type || "").includes("Mask"),
  );
  if (maskNode) {
    const inp = maskNode[1].inputs || {};
    map.mask = { node: maskNode[0], input: "image" in inp ? "image" : "mask" };
  }
  const sampler = entries.find(([, n]) =>
    (n.class_type || "").includes("KSampler"),
  );
  if (sampler) {
    const inp = sampler[1].inputs || {};
    map.seed = {
      node: sampler[0],
      input: "seed" in inp ? "seed" : "noise_seed" in inp ? "noise_seed" : "",
    };
  }
  return map;
}

export default function WorkflowPanel() {
  const t = useTranslations("studio.workflow");
  const [items, setItems] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Workflow | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [showImport, setShowImport] = useState(false);
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
        <button
          onClick={() => setShowImport(true)}
          className="w-full px-2 py-1.5 rounded bg-sky-500/15 hover:bg-sky-500/25 text-sky-200 text-xs font-medium"
        >
          {t("import_open")}
        </button>
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

      {showImport && (
        <ImportDialog
          onClose={() => setShowImport(false)}
          onCreated={async () => {
            setShowImport(false);
            await load();
          }}
        />
      )}
    </div>
  );
}

/** Import a ComfyUI API-format graph as a new (custom) workflow: paste JSON,
 * name it, pick the mode (operation), and map the logical inject keys
 * (prompt/image/mask/seed/negative) to graph nodes so the engine can inject
 * user values at generation time. */
function ImportDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
}) {
  const t = useTranslations("studio.workflow");
  const [raw, setRaw] = useState("");
  const [title, setTitle] = useState("");
  const [operation, setOperation] = useState("edit");
  const [category, setCategory] = useState("edit");
  const [map, setMap] = useState<
    Record<InjectKey, { node: string; input: string }>
  >(
    () =>
      Object.fromEntries(
        INJECT_KEYS.map((k) => [k, { node: "", input: "" }]),
      ) as Record<InjectKey, { node: string; input: string }>,
  );
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Parse the pasted JSON into a node graph (memoised, no side effects):
  // `nodes` is null until it's valid; `error` carries a translated reason.
  const { nodes: graph, error: parseErr } = useMemo<{
    nodes: Record<string, ComfyNode> | null;
    error: string | null;
  }>(() => {
    if (!raw.trim()) return { nodes: null, error: null };
    try {
      const obj = JSON.parse(raw);
      // ComfyUI's "Save (API format)" export can be either the bare node map
      // ({id: {...}}) or wrapped as {"prompt": {...}}. Accept both.
      const nodes = (obj?.prompt ?? obj) as Record<string, ComfyNode>;
      const ok =
        nodes &&
        typeof nodes === "object" &&
        Object.values(nodes).some((n) => n && (n as ComfyNode).class_type);
      return ok
        ? { nodes, error: null }
        : { nodes: null, error: t("import_err_nodes") };
    } catch {
      return { nodes: null, error: t("import_err_json") };
    }
  }, [raw, t]);

  function runAutodetect() {
    if (graph) setMap(autodetectMap(graph));
  }

  const nodeIds = graph ? Object.keys(graph) : [];

  async function save() {
    if (!graph) {
      setErr(t("import_err_json"));
      return;
    }
    if (!title.trim()) {
      setErr(t("import_err_title"));
      return;
    }
    // Keep only fully-specified mappings (both node and input chosen).
    const injectMap: Record<string, { node: string; input: string }> = {};
    for (const k of INJECT_KEYS) {
      const m = map[k];
      if (m.node && m.input) {
        if (!graph[m.node]) {
          setErr(t("import_err_map", { key: k }));
          return;
        }
        injectMap[k] = { node: m.node, input: m.input };
      }
    }
    const slug =
      title
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "_")
        .replace(/^_+|_+$/g, "")
        .slice(0, 40) || "workflow";
    setSaving(true);
    setErr(null);
    try {
      await createWorkflow({
        key: `${slug}_${Math.random().toString(36).slice(2, 8)}`,
        title: title.trim(),
        operation,
        category: category.trim() || operation,
        graph: graph as Record<string, unknown>,
        inject_map: injectMap,
        params_schema: {},
      });
      onCreated();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setSaving(false);
    }
  }

  const OPERATIONS = ["edit", "generate", "inpaint", "cleanup", "eskd"];

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4">
      <div className="w-full max-w-lg rounded-lg border border-white/10 bg-zinc-900 p-4 space-y-3 my-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-zinc-100">
            {t("import_title")}
          </h3>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 text-sm px-1"
            aria-label={t("close")}
          >
            ✕
          </button>
        </div>

        <p className="text-[11px] text-zinc-500">{t("import_hint")}</p>

        <div>
          <label className="text-xs text-zinc-500">
            {t("import_json_label")}
          </label>
          <textarea
            value={raw}
            onChange={(e) => setRaw(e.target.value)}
            rows={7}
            placeholder={t("import_json_placeholder")}
            className="w-full rounded bg-zinc-950 border border-white/10 p-2 text-xs font-mono text-zinc-200"
          />
          {parseErr && <p className="text-[11px] text-red-400">{parseErr}</p>}
          {graph && (
            <p className="text-[11px] text-emerald-500">
              {t("import_nodes_ok", { count: nodeIds.length })}
            </p>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div>
            <label className="text-xs text-zinc-500">
              {t("import_name_label")}
            </label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full rounded bg-zinc-950 border border-white/10 p-2 text-sm text-zinc-200"
            />
          </div>
          <div>
            <label className="text-xs text-zinc-500">
              {t("import_operation_label")}
            </label>
            <select
              value={operation}
              onChange={(e) => {
                setOperation(e.target.value);
                setCategory(e.target.value);
              }}
              className="w-full bg-zinc-800 rounded px-2 py-2 text-sm text-white"
            >
              {OPERATIONS.map((o) => (
                <option key={o} value={o}>
                  {t.has(`op_${o}`) ? t(`op_${o}`) : o}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-xs text-zinc-500">
              {t("import_map_label")}
            </label>
            <button
              type="button"
              onClick={runAutodetect}
              disabled={!graph}
              className="text-xs text-sky-400 hover:text-sky-300 disabled:opacity-40"
            >
              {t("import_autodetect")}
            </button>
          </div>
          <p className="text-[11px] text-zinc-600 mb-1.5">
            {t("import_map_hint")}
          </p>
          <div className="space-y-1.5">
            {INJECT_KEYS.map((k) => {
              const inputs =
                graph && map[k].node && graph[map[k].node]?.inputs
                  ? Object.keys(graph[map[k].node].inputs as object)
                  : [];
              return (
                <div
                  key={k}
                  className="grid grid-cols-[70px_1fr_1fr] gap-1.5 items-center"
                >
                  <span className="text-xs text-zinc-400">
                    {t.has(`inject_${k}`) ? t(`inject_${k}`) : k}
                  </span>
                  <select
                    value={map[k].node}
                    disabled={!graph}
                    onChange={(e) =>
                      setMap((m) => ({
                        ...m,
                        [k]: { node: e.target.value, input: "" },
                      }))
                    }
                    className="bg-zinc-800 rounded px-1.5 py-1 text-xs text-white disabled:opacity-40"
                  >
                    <option value="">{t("import_node_none")}</option>
                    {nodeIds.map((id) => (
                      <option key={id} value={id}>
                        {id} — {graph?.[id]?.class_type ?? "?"}
                      </option>
                    ))}
                  </select>
                  <select
                    value={map[k].input}
                    disabled={!map[k].node}
                    onChange={(e) =>
                      setMap((m) => ({
                        ...m,
                        [k]: { ...m[k], input: e.target.value },
                      }))
                    }
                    className="bg-zinc-800 rounded px-1.5 py-1 text-xs text-white disabled:opacity-40"
                  >
                    <option value="">{t("import_input_none")}</option>
                    {inputs.map((inp) => (
                      <option key={inp} value={inp}>
                        {inp}
                      </option>
                    ))}
                  </select>
                </div>
              );
            })}
          </div>
        </div>

        {err && <div className="text-xs text-red-400">{err}</div>}

        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
          >
            {t("import_cancel")}
          </button>
          <button
            onClick={save}
            disabled={saving || !graph || !title.trim()}
            className="px-3 py-1.5 rounded bg-sky-600 hover:bg-sky-500 text-white text-sm disabled:opacity-50"
          >
            {saving ? t("import_saving") : t("import_save")}
          </button>
        </div>
      </div>
    </div>
  );
}
