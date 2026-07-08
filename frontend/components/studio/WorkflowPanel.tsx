"use client";

import { useTranslations } from "next-intl";
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from "react";

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
const INJECT_KEYS = [
  "prompt",
  "negative",
  "image",
  "mask",
  "seed",
  "width",
  "height",
  "steps",
  "cfg",
  "denoise",
  "guidance",
  "controlnet_image",
  "controlnet_strength",
] as const;
type InjectKey = (typeof INJECT_KEYS)[number];
type ComfyNode = { class_type?: string; inputs?: Record<string, unknown> };
type InjectTarget = { node: string; input: string };
type InjectMap = Record<InjectKey, InjectTarget>;

const EMPTY_TARGET: InjectTarget = { node: "", input: "" };
const WORKFLOW_OPERATIONS = ["edit", "generate", "inpaint", "cleanup", "eskd"];

function emptyInjectMap(): InjectMap {
  return Object.fromEntries(
    INJECT_KEYS.map((k) => [k, { ...EMPTY_TARGET }]),
  ) as InjectMap;
}

function className(n: ComfyNode): string {
  return String(n.class_type || "");
}

function lowerClass(n: ComfyNode): string {
  return className(n).toLowerCase();
}

function inputKeys(node: ComfyNode | undefined): string[] {
  return Object.keys(node?.inputs || {});
}

function firstInput(node: ComfyNode, candidates: string[]): string {
  const keys = inputKeys(node);
  return candidates.find((k) => keys.includes(k)) || "";
}

function setIfInput(
  map: InjectMap,
  key: InjectKey,
  nodeId: string,
  node: ComfyNode,
  candidates: string[],
) {
  if (map[key].node) return;
  const input = firstInput(node, candidates);
  if (input) map[key] = { node: nodeId, input };
}

function looksNegativeText(node: ComfyNode): boolean {
  const text = String(node.inputs?.text || node.inputs?.string || "").toLowerCase();
  return /negative|worst|bad|blur|artifact|low quality|deformed|лишн|размыт|артефакт/.test(text);
}

function widgetInputsForNode(
  node: { type?: string; class_type?: string; widgets_values?: unknown },
): Record<string, unknown> {
  const values = node.widgets_values;
  if (!Array.isArray(values)) {
    return values && typeof values === "object" ? (values as Record<string, unknown>) : {};
  }
  const type = String(node.type || node.class_type || "");
  const low = type.toLowerCase();
  const out: Record<string, unknown> = {};
  if (low.includes("cliptextencode") || low.includes("text")) {
    if (values[0] !== undefined) out.text = values[0];
  } else if (low.includes("ksampler")) {
    const names = ["seed", "control_after_generate", "steps", "cfg", "sampler_name", "scheduler", "denoise"];
    names.forEach((name, idx) => {
      if (values[idx] !== undefined) out[name] = values[idx];
    });
  } else if (low.includes("randomnoise")) {
    if (values[0] !== undefined) out.noise_seed = values[0];
  } else if (low.includes("emptylatent") || low.includes("emptyflux") || low.includes("emptysd3")) {
    const names = ["width", "height", "batch_size"];
    names.forEach((name, idx) => {
      if (values[idx] !== undefined) out[name] = values[idx];
    });
  } else if (low.includes("loadimage")) {
    if (values[0] !== undefined) out.image = values[0];
  } else if (low.includes("saveimage")) {
    if (values[0] !== undefined) out.filename_prefix = values[0];
  }
  return out;
}

function normalizeWorkflowJson(obj: unknown): Record<string, ComfyNode> | null {
  const root = obj as Record<string, unknown> | null;
  if (!root || typeof root !== "object") return null;
  const wrapped = (root.prompt ?? root.workflow ?? root) as Record<string, unknown>;
  if (
    wrapped &&
    typeof wrapped === "object" &&
    !Array.isArray(wrapped) &&
    Object.values(wrapped).some((n) => n && typeof n === "object" && "class_type" in (n as object))
  ) {
    return wrapped as Record<string, ComfyNode>;
  }

  const visualRoot = Array.isArray(root.nodes)
    ? root
    : Array.isArray((root.workflow as Record<string, unknown> | undefined)?.nodes)
      ? (root.workflow as Record<string, unknown>)
      : null;
  if (!visualRoot) return null;
  const nodes = visualRoot.nodes as Array<Record<string, unknown>>;

  const apiGraph: Record<string, ComfyNode> = {};
  for (const node of nodes) {
    const id = String(node.id ?? "");
    const type = String(node.type ?? node.class_type ?? "");
    if (!id || !type) continue;
    const inputs: Record<string, unknown> = widgetInputsForNode(node);
    const uiInputs = Array.isArray(node.inputs)
      ? (node.inputs as Array<Record<string, unknown>>)
      : [];
    for (const inp of uiInputs) {
      const name = String(inp.name ?? "");
      if (!name) continue;
      const link = inp.link;
      if (link !== null && link !== undefined) {
        const linkArr = Array.isArray(visualRoot.links)
          ? (visualRoot.links as unknown[]).find((l) => Array.isArray(l) && String((l as unknown[])[0]) === String(link))
          : null;
        if (Array.isArray(linkArr)) {
          inputs[name] = [String(linkArr[1]), Number(linkArr[2]) || 0];
        }
      }
    }
    apiGraph[id] = { class_type: type, inputs };
  }
  return Object.keys(apiGraph).length ? apiGraph : null;
}

/** Best-effort prefill of the node/input for each logical key from a pasted
 * ComfyUI API graph — the user can correct any of it before saving. */
function autodetectMap(graph: Record<string, ComfyNode>): InjectMap {
  const map = emptyInjectMap();
  const entries = Object.entries(graph);
  const textNodes = entries.filter(([, n]) => {
    const c = lowerClass(n);
    return c.includes("cliptextencode") || c.includes("text") || firstInput(n, ["text", "string", "prompt"]);
  });
  const negative = textNodes.find(([, n]) => looksNegativeText(n));
  const positive = textNodes.find(([id]) => id !== negative?.[0]);
  if (positive) setIfInput(map, "prompt", positive[0], positive[1], ["text", "string", "prompt"]);
  if (negative) setIfInput(map, "negative", negative[0], negative[1], ["text", "string", "prompt"]);
  if (!map.negative.node && textNodes.length > 1) {
    setIfInput(map, "negative", textNodes[1][0], textNodes[1][1], ["text", "string", "prompt"]);
  }

  for (const [id, node] of entries) {
    const c = lowerClass(node);
    if (c.includes("loadimage") || c.includes("imageinput") || c.includes("image load")) {
      setIfInput(map, "image", id, node, ["image", "path", "filename"]);
    }
    if (c.includes("mask") || inputKeys(node).some((k) => k.toLowerCase().includes("mask"))) {
      setIfInput(map, "mask", id, node, ["mask", "image"]);
    }
    if (c.includes("randomnoise") || c.includes("ksampler") || c.includes("sampler")) {
      setIfInput(map, "seed", id, node, ["seed", "noise_seed"]);
    }
    setIfInput(map, "width", id, node, ["width"]);
    setIfInput(map, "height", id, node, ["height"]);
    setIfInput(map, "steps", id, node, ["steps"]);
    setIfInput(map, "cfg", id, node, ["cfg"]);
    setIfInput(map, "denoise", id, node, ["denoise"]);
    setIfInput(map, "guidance", id, node, ["guidance"]);
    if (c.includes("controlnet")) {
      setIfInput(map, "controlnet_image", id, node, ["image", "control_net_image"]);
      setIfInput(map, "controlnet_strength", id, node, ["strength"]);
    }
  }
  return map;
}

function injectMapFromWorkflow(raw: Record<string, unknown>): InjectMap {
  const map = emptyInjectMap();
  for (const key of INJECT_KEYS) {
    const val = raw[key];
    const target = Array.isArray(val) ? val[0] : val;
    if (target && typeof target === "object") {
      const rec = target as Record<string, unknown>;
      map[key] = {
        node: rec.node !== undefined ? String(rec.node) : "",
        input: rec.input !== undefined ? String(rec.input) : "",
      };
    }
  }
  return map;
}

function compactInjectMap(map: InjectMap, graph: Record<string, ComfyNode>): Record<string, InjectTarget> {
  const injectMap: Record<string, InjectTarget> = {};
  for (const k of INJECT_KEYS) {
    const m = map[k];
    if (!m.node && !m.input) continue;
    if (!m.node || !m.input || !graph[m.node]) {
      throw new Error(`bad-map:${k}`);
    }
    injectMap[k] = { node: m.node, input: m.input };
  }
  return injectMap;
}

function MappingFields({
  graph,
  map,
  setMap,
}: {
  graph: Record<string, ComfyNode> | null;
  map: InjectMap;
  setMap: Dispatch<SetStateAction<InjectMap>>;
}) {
  const t = useTranslations("studio.workflow");
  const nodeIds = graph ? Object.keys(graph) : [];
  return (
    <div className="space-y-1.5">
      {INJECT_KEYS.map((k) => {
        const inputs =
          graph && map[k].node && graph[map[k].node]?.inputs
            ? Object.keys(graph[map[k].node].inputs as object)
            : [];
        return (
          <div
            key={k}
            className="grid grid-cols-[88px_1fr_1fr] gap-1.5 items-center"
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
                  {id} - {graph?.[id]?.class_type ?? "?"}
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
  );
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
  const [editing, setEditing] = useState<Workflow | null>(null);
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
                {!selected.is_builtin && (
                  <button
                    onClick={() => setEditing(selected)}
                    className="px-2.5 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
                  >
                    {t("edit")}
                  </button>
                )}
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
      {editing && (
        <EditWorkflowDialog
          workflow={editing}
          onClose={() => setEditing(null)}
          onSaved={async (wf) => {
            setEditing(null);
            setSelected(wf);
            await load();
          }}
        />
      )}
    </div>
  );
}

function EditWorkflowDialog({
  workflow,
  onClose,
  onSaved,
}: {
  workflow: Workflow;
  onClose: () => void;
  onSaved: (wf: Workflow) => void;
}) {
  const t = useTranslations("studio.workflow");
  const [title, setTitle] = useState(workflow.title);
  const [description, setDescription] = useState(workflow.description ?? "");
  const [operation, setOperation] = useState(workflow.operation || "edit");
  const [category, setCategory] = useState(workflow.category || workflow.operation || "edit");
  const [graphRaw, setGraphRaw] = useState(JSON.stringify(workflow.graph || {}, null, 2));
  const [paramsRaw, setParamsRaw] = useState(JSON.stringify(workflow.params_schema || {}, null, 2));
  const [map, setMap] = useState<InjectMap>(() =>
    injectMapFromWorkflow(workflow.inject_map || {}),
  );
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const { graph, error: graphErr } = useMemo<{
    graph: Record<string, ComfyNode> | null;
    error: string | null;
  }>(() => {
    try {
      const parsed = JSON.parse(graphRaw || "{}");
      const normalized = normalizeWorkflowJson(parsed);
      return normalized
        ? { graph: normalized, error: null }
        : { graph: null, error: t("import_err_nodes") };
    } catch {
      return { graph: null, error: t("import_err_json") };
    }
  }, [graphRaw, t]);

  function runAutodetect() {
    if (graph) setMap(autodetectMap(graph));
  }

  async function save() {
    if (!graph) {
      setErr(graphErr || t("import_err_json"));
      return;
    }
    if (!title.trim()) {
      setErr(t("import_err_title"));
      return;
    }
    let paramsSchema: Record<string, unknown>;
    try {
      const parsed = JSON.parse(paramsRaw || "{}");
      paramsSchema = parsed && typeof parsed === "object" && !Array.isArray(parsed)
        ? (parsed as Record<string, unknown>)
        : {};
    } catch {
      setErr(t("edit_err_params_json"));
      return;
    }
    let injectMap: Record<string, InjectTarget>;
    try {
      injectMap = compactInjectMap(map, graph);
    } catch (e) {
      const key = String((e as Error).message || "").replace("bad-map:", "") || "?";
      setErr(t("import_err_map", { key }));
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      const updated = await patchWorkflow(workflow.id, {
        title: title.trim(),
        description: description.trim() || null,
        operation,
        category: category.trim() || operation,
        graph,
        inject_map: injectMap,
        params_schema: paramsSchema,
      });
      onSaved(updated);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/60 p-4">
      <div className="w-full max-w-3xl rounded-lg border border-white/10 bg-zinc-900 p-4 space-y-3 my-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold text-zinc-100">
            {t("edit_title")}
          </h3>
          <button
            onClick={onClose}
            className="text-zinc-500 hover:text-zinc-200 text-sm px-1"
            aria-label={t("close")}
          >
            x
          </button>
        </div>

        <div className="grid gap-2 sm:grid-cols-2">
          <div>
            <label className="text-xs text-zinc-500">{t("import_name_label")}</label>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="w-full rounded bg-zinc-950 border border-white/10 p-2 text-sm text-zinc-200"
            />
          </div>
          <div>
            <label className="text-xs text-zinc-500">{t("edit_category_label")}</label>
            <input
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full rounded bg-zinc-950 border border-white/10 p-2 text-sm text-zinc-200"
            />
          </div>
          <div>
            <label className="text-xs text-zinc-500">{t("import_operation_label")}</label>
            <select
              value={operation}
              onChange={(e) => {
                setOperation(e.target.value);
                if (!category.trim()) setCategory(e.target.value);
              }}
              className="w-full bg-zinc-800 rounded px-2 py-2 text-sm text-white"
            >
              {WORKFLOW_OPERATIONS.map((o) => (
                <option key={o} value={o}>
                  {t.has(`op_${o}`) ? t(`op_${o}`) : o}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="text-xs text-zinc-500">{t("edit_description_label")}</label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              className="w-full rounded bg-zinc-950 border border-white/10 p-2 text-sm text-zinc-200"
            />
          </div>
        </div>

        <div className="grid gap-3 lg:grid-cols-2">
          <div>
            <label className="text-xs text-zinc-500">{t("edit_graph_label")}</label>
            <textarea
              value={graphRaw}
              onChange={(e) => setGraphRaw(e.target.value)}
              rows={14}
              className="w-full rounded bg-zinc-950 border border-white/10 p-2 text-xs font-mono text-zinc-200"
            />
            {graphErr && <p className="text-[11px] text-red-400">{graphErr}</p>}
            {graph && (
              <p className="text-[11px] text-emerald-500">
                {t("import_nodes_ok", { count: Object.keys(graph).length })}
              </p>
            )}
          </div>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-zinc-500">{t("edit_params_label")}</label>
              <textarea
                value={paramsRaw}
                onChange={(e) => setParamsRaw(e.target.value)}
                rows={5}
                className="w-full rounded bg-zinc-950 border border-white/10 p-2 text-xs font-mono text-zinc-200"
              />
            </div>
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-zinc-500">{t("import_map_label")}</label>
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
              <MappingFields graph={graph} map={map} setMap={setMap} />
            </div>
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
            {saving ? t("edit_saving") : t("edit_save")}
          </button>
        </div>
      </div>
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
  >(() => emptyInjectMap());
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
      // ({id: {...}}), wrapped as {"prompt": {...}}, or the visual editor
      // format ({nodes, links}). Accept all and normalize to prompt/API graph.
      const nodes = normalizeWorkflowJson(obj);
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
    let injectMap: Record<string, InjectTarget>;
    try {
      injectMap = compactInjectMap(map, graph);
    } catch (e) {
      const key = String((e as Error).message || "").replace("bad-map:", "") || "?";
      setErr(t("import_err_map", { key }));
      return;
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
              {WORKFLOW_OPERATIONS.map((o) => (
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
          <MappingFields graph={graph} map={map} setMap={setMap} />
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
