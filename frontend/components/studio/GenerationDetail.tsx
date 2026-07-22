"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import {
  Generation,
  GenerateInput,
  Workflow,
  acceptGeneration,
  artifactUrl,
  deleteGeneration,
  duplicateWorkflow,
  iterateGeneration,
  listWorkflows,
  patchWorkflow,
  promptHelp,
  resultUrl,
  sourceUrl,
} from "@/lib/studio-api";

interface Props {
  gen: Generation;
  onChanged: () => void;
  onClose: () => void;
}

export default function GenerationDetail({ gen, onChanged, onClose }: Props) {
  const t = useTranslations("studio");
  const [iterPrompt, setIterPrompt] = useState("");
  const [iterNegative, setIterNegative] = useState("");
  const [iterSeed, setIterSeed] = useState("0");
  const [busy, setBusy] = useState(false);
  const [helping, setHelping] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const hasSource = (gen.source_image_paths?.length ?? 0) > 0;

  // Iteration = edit with an instruction. The "fast" (Lightning 4-step) preset
  // never actually performs the edit — default to "quality" so the prompt is
  // followed. Let custom (trained-LoRA) edit workflows carry their own config.
  const [quality, setQuality] = useState<"fast" | "quality">("quality");
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflowId, setWorkflowId] = useState<string>("");
  const [workflowParamOverrides, setWorkflowParamOverrides] = useState<
    Record<string, Record<string, unknown>>
  >({});
  const [paramSaveMsg, setParamSaveMsg] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const hasDxf = typeof gen.params?.dxf_path === "string";
  const graphReadAttempt = gen.params?.drawing_graph_read_attempt as
    | {
        raw_sha256?: string;
        reader_manifest?: { model?: string; provider?: string; contract?: string };
        validation_errors?: Array<{
          type?: string;
          msg?: string;
          loc?: Array<string | number>;
        }>;
      }
    | undefined;
  const selectedWorkflow = workflows.find((w) => w.id === workflowId) ?? null;
  const selectedParamsSchema = (selectedWorkflow?.params_schema ||
    {}) as Record<string, Record<string, unknown>>;
  const workflowParamEntries = Object.entries(selectedParamsSchema).filter(
    ([, spec]) => spec && typeof spec === "object",
  );
  const showQuality =
    !!selectedWorkflow &&
    selectedWorkflow.is_builtin &&
    !!(selectedWorkflow.inject_map as Record<string, unknown>)?.lora_strength;

  useEffect(() => {
    listWorkflows()
      .then((ws) =>
        setWorkflows(ws.filter((w) => w.enabled && w.operation === "edit")),
      )
      .catch(() => undefined);
  }, []);
  // Default to the newest custom edit workflow (as the composer does), otherwise
  // use the first builtin edit pipeline.
  useEffect(() => {
    const custom = workflows.find((w) => !w.is_builtin);
    setWorkflowId(custom ? custom.id : (workflows[0]?.id ?? ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflows.length]);

  async function reloadWorkflows(): Promise<Workflow[]> {
    const ws = (await listWorkflows()).filter(
      (w) => w.enabled && w.operation === "edit",
    );
    setWorkflows(ws);
    return ws;
  }

  async function run(fn: () => Promise<unknown>) {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function submitIterate() {
    const input: GenerateInput = {
      operation: "edit",
      prompt: iterPrompt,
      negative_prompt: iterNegative || undefined,
      params: { seed: Number(iterSeed) || 0 },
    };
    if (workflowId) {
      input.workflow_id = workflowId;
    }
    if (showQuality) {
      (input.params as Record<string, unknown>).quality = quality;
    }
    Object.assign(
      input.params as Record<string, unknown>,
      collectWorkflowParams(),
    );
    await iterateGeneration(gen.id, input);
    setIterPrompt("");
  }

  function workflowTextValue(logicalKey: "prompt" | "negative"): string {
    const wf = selectedWorkflow;
    if (!wf) return "";
    const inject = wf.inject_map as Record<string, unknown>;
    const rawTarget = inject?.[logicalKey];
    const target = Array.isArray(rawTarget) ? rawTarget[0] : rawTarget;
    if (target && typeof target === "object") {
      const rec = target as Record<string, unknown>;
      const nodeId = String(rec.node ?? "");
      const input = String(rec.input ?? "");
      const node = (
        wf.graph as Record<string, { inputs?: Record<string, unknown> }>
      )[nodeId];
      const value = node?.inputs?.[input];
      if (typeof value === "string" && value.trim()) return value;
    }
    for (const node of Object.values(
      wf.graph as Record<
        string,
        { class_type?: string; inputs?: Record<string, unknown> }
      >,
    )) {
      const cls = String(node?.class_type || "").toLowerCase();
      if (!cls.includes("text") && !cls.includes("clip")) continue;
      for (const input of ["prompt", "text", "string"]) {
        const value = node.inputs?.[input];
        if (typeof value === "string" && value.trim()) return value;
      }
    }
    return "";
  }

  function showIterPrompt() {
    setIterPrompt(
      iterPrompt.trim() || workflowTextValue("prompt") || gen.prompt || "",
    );
    const negative = workflowTextValue("negative");
    if (negative && !iterNegative.trim()) setIterNegative(negative);
  }

  async function helpWithIterPrompt() {
    if (!iterPrompt.trim()) return;
    setHelping(true);
    setErr(null);
    try {
      const res = await promptHelp(iterPrompt, "edit");
      if (res.prompt) setIterPrompt(res.prompt);
      if (res.negative_prompt) setIterNegative(res.negative_prompt);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setHelping(false);
    }
  }

  function workflowParamValue(key: string, spec: Record<string, unknown>) {
    if (!selectedWorkflow) return spec.default ?? "";
    const saved = workflowParamOverrides[selectedWorkflow.id]?.[key];
    return saved !== undefined ? saved : (spec.default ?? "");
  }

  function setWorkflowParamValue(key: string, value: unknown) {
    if (!selectedWorkflow) return;
    setParamSaveMsg(null);
    setWorkflowParamOverrides((cur) => ({
      ...cur,
      [selectedWorkflow.id]: {
        ...(cur[selectedWorkflow.id] || {}),
        [key]: value,
      },
    }));
  }

  function castWorkflowParam(value: unknown, spec: Record<string, unknown>) {
    const type = String(spec.type || "");
    if (type === "bool" || type === "boolean") return Boolean(value);
    if (type === "int" || type === "integer") {
      const n = Number(value);
      return Number.isFinite(n) ? Math.round(n) : undefined;
    }
    if (type === "float" || type === "number") {
      const n = Number(value);
      return Number.isFinite(n) ? n : undefined;
    }
    return value === "" ? undefined : value;
  }

  function collectWorkflowParams(): Record<string, unknown> {
    if (!selectedWorkflow) return {};
    const out: Record<string, unknown> = {};
    for (const [key, spec] of workflowParamEntries) {
      const casted = castWorkflowParam(workflowParamValue(key, spec), spec);
      if (casted !== undefined) out[key] = casted;
    }
    return out;
  }

  async function saveWorkflowParamDefaults() {
    if (!selectedWorkflow) return;
    setBusy(true);
    setErr(null);
    setParamSaveMsg(null);
    try {
      let target = selectedWorkflow;
      if (selectedWorkflow.is_builtin) {
        target = await duplicateWorkflow(selectedWorkflow.id);
      }
      const nextSchema: Record<string, unknown> = {};
      for (const [key, spec] of Object.entries(selectedParamsSchema)) {
        const nextSpec: Record<string, unknown> =
          spec && typeof spec === "object" ? { ...spec } : { type: "string" };
        const casted = castWorkflowParam(
          workflowParamValue(key, nextSpec),
          nextSpec,
        );
        if (casted !== undefined) nextSpec.default = casted;
        nextSchema[key] = nextSpec;
      }
      const updated = await patchWorkflow(target.id, {
        params_schema: nextSchema,
      });
      await reloadWorkflows();
      setWorkflowId(updated.id);
      setWorkflowParamOverrides((cur) => {
        const next = { ...cur };
        delete next[selectedWorkflow.id];
        delete next[updated.id];
        return next;
      });
      setParamSaveMsg(
        selectedWorkflow.is_builtin
          ? t("composer.workflow_params_saved_copy")
          : t("composer.workflow_params_saved"),
      );
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function addOwnWorkflow() {
    const base = selectedWorkflow || workflows.find((w) => w.is_builtin);
    if (!base) return;
    setErr(null);
    try {
      const copy = await duplicateWorkflow(base.id);
      await reloadWorkflows();
      setWorkflowId(copy.id);
      setRenaming(copy.id);
      setRenameValue(copy.title);
    } catch (e) {
      setErr(String((e as Error).message || e));
    }
  }

  async function saveRename() {
    if (!renaming) return;
    try {
      await patchWorkflow(renaming, { title: renameValue.trim() || undefined });
      await reloadWorkflows();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setRenaming(null);
    }
  }

  function workflowParamsEditor() {
    if (!selectedWorkflow || workflowParamEntries.length === 0) return null;
    return (
      <div className="space-y-2 border-t border-white/10 pt-2">
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="text-xs text-zinc-500">
              {t("composer.workflow_params_label")}
            </div>
            <div className="text-[11px] text-zinc-600">
              {selectedWorkflow.is_builtin
                ? t("composer.workflow_params_builtin_hint")
                : t("composer.workflow_params_hint")}
            </div>
          </div>
          <button
            type="button"
            onClick={saveWorkflowParamDefaults}
            disabled={busy}
            className="shrink-0 rounded bg-white/10 px-2 py-1 text-xs hover:bg-white/20 disabled:opacity-50"
          >
            {t("composer.workflow_params_save")}
          </button>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          {workflowParamEntries.map(([key, spec]) => {
            const type = String(spec.type || "");
            const label = String(spec.label || key);
            const value = workflowParamValue(key, spec);
            const options = Array.isArray(spec.options) ? spec.options : null;
            if (type === "bool" || type === "boolean") {
              return (
                <label
                  key={key}
                  className="flex items-center gap-2 rounded bg-zinc-900/80 border border-white/10 p-2 text-xs text-zinc-300"
                >
                  <input
                    type="checkbox"
                    checked={Boolean(value)}
                    onChange={(e) =>
                      setWorkflowParamValue(key, e.target.checked)
                    }
                  />
                  <span>{label}</span>
                </label>
              );
            }
            if (options) {
              return (
                <label key={key} className="block">
                  <span className="text-xs text-zinc-500">{label}</span>
                  <select
                    value={String(value ?? "")}
                    onChange={(e) => setWorkflowParamValue(key, e.target.value)}
                    className="mt-1 w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                  >
                    {options.map((opt) => (
                      <option key={String(opt)} value={String(opt)}>
                        {String(opt)}
                      </option>
                    ))}
                  </select>
                </label>
              );
            }
            const numeric = ["int", "integer", "float", "number"].includes(
              type,
            );
            return (
              <label key={key} className="block">
                <span className="text-xs text-zinc-500">{label}</span>
                <input
                  type={numeric ? "number" : "text"}
                  value={String(value ?? "")}
                  min={spec.min !== undefined ? Number(spec.min) : undefined}
                  max={spec.max !== undefined ? Number(spec.max) : undefined}
                  step={
                    type === "int" || type === "integer"
                      ? 1
                      : numeric
                        ? 0.1
                        : undefined
                  }
                  onChange={(e) => setWorkflowParamValue(key, e.target.value)}
                  className="mt-1 w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                />
              </label>
            );
          })}
        </div>
        {paramSaveMsg && (
          <div className="text-[11px] text-emerald-400">{paramSaveMsg}</div>
        )}
      </div>
    );
  }

  const canAct =
    gen.status === "failed" ||
    gen.status === "done" ||
    gen.status === "cancelled";

  // Vectorized (scan→DXF) drawings live in the standalone CAD editor at
  // /cad/[id] — this panel only offers the jump there (plus delete), since
  // diffusion iterate/workflow controls make no sense for a CAD result and
  // the full editor no longer embeds into the studio overlay.
  if (gen.operation === "vectorize" && gen.status === "done") {
    return (
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-zinc-200">
            {t("vector.title")} · {t(`status.${gen.status}`)}
            {gen.accepted ? t("status.accepted_suffix") : ""}
          </h3>
          <button
            onClick={onClose}
            className="text-zinc-400 hover:text-white text-sm px-2 -mr-2"
          >
            ✕
          </button>
        </div>
        <Link
          href={`/cad/${gen.id}`}
          className="rounded bg-sky-600 px-3 py-2 text-center text-sm text-white hover:bg-sky-500"
        >
          {t("vector.open_in_cad")}
        </Link>
        {canAct && (
          <button
            disabled={busy}
            onClick={() => {
              if (confirm(t("detail.delete_confirm"))) {
                run(() =>
                  deleteGeneration(gen.id).then(onChanged).then(onClose),
                );
              }
            }}
            className="self-end text-xs text-red-300 hover:text-red-200 disabled:opacity-50"
          >
            {t("detail.delete")}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-zinc-200">
          {gen.operation} · {t(`status.${gen.status}`)}
          {gen.accepted ? t("status.accepted_suffix") : ""}
        </h3>
        <button
          onClick={onClose}
          className="text-zinc-400 hover:text-white text-sm px-2 -mr-2"
        >
          ✕
        </button>
      </div>

      {/* Primary actions up top so they're reachable without scrolling past the
          (tall) result image — important on mobile where the panel is an
          overlay. Delete is available for any generation (incl. failed). */}
      {(gen.has_result || canAct) && (
        <div className="flex flex-wrap items-center gap-2">
          {gen.has_result && !gen.accepted && (
            <button
              disabled={busy}
              onClick={() => run(() => acceptGeneration(gen.id))}
              className="px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm disabled:opacity-50"
            >
              {t("detail.accept")}
            </button>
          )}
          {gen.has_result && (
            <a
              href={resultUrl(gen.id)}
              download={`studio-${gen.id}.png`}
              className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
            >
              {t("detail.download")}
            </a>
          )}
          {hasDxf && (
            <a
              href={artifactUrl(gen.id, "dxf")}
              download={`studio-${gen.id}.dxf`}
              className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
            >
              {t("detail.download_dxf")}
            </a>
          )}
          {canAct && (
            <button
              disabled={busy}
              onClick={() => {
                if (confirm(t("detail.delete_confirm"))) {
                  run(() =>
                    deleteGeneration(gen.id).then(onChanged).then(onClose),
                  );
                }
              }}
              className="ml-auto text-xs text-red-300 hover:text-red-200 disabled:opacity-50"
            >
              {t("detail.delete")}
            </button>
          )}
        </div>
      )}

      {gen.status === "failed" && (
        <div className="space-y-2">
          <div className="text-xs text-red-400 bg-red-500/10 rounded p-2 whitespace-pre-wrap max-h-40 overflow-y-auto">
            {gen.error}
          </div>
          {graphReadAttempt && (
            <details className="rounded border border-amber-400/20 bg-amber-950/20 p-2 text-xs text-amber-100">
              <summary className="cursor-pointer font-medium">
                {t("detail.graph_reader_diagnostics", {
                  count: graphReadAttempt.validation_errors?.length ?? 0,
                })}
              </summary>
              <div className="mt-2 space-y-1 text-[11px]">
                <div>{t("detail.graph_reader_model")}: {graphReadAttempt.reader_manifest?.model ?? "—"}</div>
                <div className="font-mono">SHA {graphReadAttempt.raw_sha256?.slice(0, 20) ?? "—"}…</div>
                <ul className="list-disc space-y-1 pl-4">
                  {graphReadAttempt.validation_errors?.slice(0, 8).map((issue, index) => (
                    <li key={`${issue.type ?? "validation"}-${index}`}>
                      <span className="font-mono">{issue.loc?.join(".") || issue.type || "validation"}</span>
                      {issue.msg ? `: ${issue.msg}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            </details>
          )}
        </div>
      )}

      {(gen.status === "running" || gen.status === "queued") && (
        <div className="space-y-1">
          <div
            className="h-2 rounded bg-zinc-800 overflow-hidden"
            role="progressbar"
            aria-valuenow={gen.progress?.pct}
          >
            <div
              className={`h-full bg-sky-500 ${
                gen.progress ? "transition-all" : "animate-pulse w-1/3"
              }`}
              style={
                gen.progress ? { width: `${gen.progress.pct}%` } : undefined
              }
            />
          </div>
          <div className="text-[11px] text-zinc-400">
            {gen.status === "queued"
              ? t("status.queued")
              : gen.progress
                ? `${t("status.running")} · ${gen.progress.value ?? 0}/${gen.progress.max ?? "?"} (${gen.progress.pct}%)`
                : t("status.running")}
          </div>
        </div>
      )}

      {(gen.source_document_id || gen.case_id) && (
        <div className="flex flex-wrap gap-2 text-[11px]">
          {gen.source_document_id && (
            <span className="px-2 py-0.5 rounded bg-white/5 text-zinc-400">
              {t("detail.doc_badge", { id: gen.source_document_id })}
            </span>
          )}
          {gen.case_id && (
            <span className="px-2 py-0.5 rounded bg-white/5 text-zinc-400">
              {t("detail.case_badge", { id: gen.case_id })}
            </span>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-2">
        {hasSource && (
          <figure>
            <figcaption className="text-[11px] text-zinc-500 mb-1">
              {t("detail.source_label")}
            </figcaption>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={sourceUrl(gen.id, 0)}
              alt={t("composer.source_alt")}
              className="w-full rounded border border-white/10 bg-zinc-900"
            />
          </figure>
        )}
        {gen.has_result && (
          <figure className={hasSource ? "" : "col-span-2"}>
            <figcaption className="text-[11px] text-zinc-500 mb-1">
              {t("detail.result_label")}
            </figcaption>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={resultUrl(gen.id)}
              alt={t("gallery.result_alt")}
              className="w-full rounded border border-white/10 bg-zinc-900"
            />
          </figure>
        )}
      </div>

      {gen.prompt && <p className="text-xs text-zinc-400">{gen.prompt}</p>}

      {err && <div className="text-xs text-red-400">{err}</div>}

      {gen.has_result && (
        <div className="border-t border-white/10 pt-3 space-y-2">
          {workflows.length > 0 && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-zinc-500">
                  {t("composer.workflow_label")}
                </label>
                <button
                  type="button"
                  onClick={addOwnWorkflow}
                  className="text-xs text-sky-400 hover:text-sky-300"
                >
                  {t("composer.workflow_add_own")}
                </button>
              </div>
              <select
                value={workflowId}
                onChange={(e) => {
                  setWorkflowId(e.target.value);
                  setRenaming(null);
                }}
                className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white"
              >
                {workflows.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.is_builtin ? w.title : `★ ${w.title}`}
                  </option>
                ))}
              </select>
              {renaming && renaming === workflowId && (
                <div className="mt-1.5 flex gap-1">
                  <input
                    value={renameValue}
                    autoFocus
                    onChange={(e) => setRenameValue(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void saveRename();
                      if (e.key === "Escape") setRenaming(null);
                    }}
                    placeholder={t("composer.workflow_rename_placeholder")}
                    className="flex-1 rounded bg-zinc-900 border border-white/10 px-2 py-1 text-sm text-zinc-200"
                  />
                  <button
                    onClick={saveRename}
                    className="px-2 py-1 rounded bg-sky-600 hover:bg-sky-500 text-white text-xs"
                  >
                    {t("composer.workflow_rename_save")}
                  </button>
                  <button
                    onClick={() => setRenaming(null)}
                    className="px-2 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
                  >
                    {t("composer.workflow_rename_cancel")}
                  </button>
                </div>
              )}
              {selectedWorkflow?.description && renaming !== workflowId && (
                <p className="mt-1 text-[11px] text-zinc-600">
                  {selectedWorkflow.description}
                </p>
              )}
            </div>
          )}

          <div>
            <div className="flex items-center justify-between mb-1">
              <label className="text-[11px] text-zinc-500">
                {t("detail.iterate_label")}
              </label>
              <button
                type="button"
                onClick={showIterPrompt}
                className="text-xs text-sky-400 hover:text-sky-300"
              >
                {t("composer.show_prompt")}
              </button>
            </div>
            <textarea
              value={iterPrompt}
              onChange={(e) => setIterPrompt(e.target.value)}
              placeholder={t("detail.iterate_placeholder")}
              className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
              rows={3}
            />
            <div className="mt-1 flex justify-end">
              <button
                onClick={helpWithIterPrompt}
                disabled={helping || !iterPrompt.trim()}
                className="text-xs text-sky-400 hover:text-sky-300 disabled:opacity-40"
              >
                {helping
                  ? t("composer.help_prompt_busy")
                  : t("composer.help_prompt")}
              </button>
            </div>
          </div>

          {/* Speed/quality — shown only for Lightning-LoRA builtins. Custom
              workflows carry their own tuned config. */}
          {showQuality && (
            <div className="grid grid-cols-2 gap-1 p-1 rounded bg-white/5">
              <button
                onClick={() => setQuality("fast")}
                className={`px-3 py-1.5 rounded text-sm ${quality === "fast" ? "bg-sky-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
              >
                {t("composer.quality_fast")}
              </button>
              <button
                onClick={() => setQuality("quality")}
                className={`px-3 py-1.5 rounded text-sm ${quality === "quality" ? "bg-emerald-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
              >
                {t("composer.quality_quality")}
              </button>
            </div>
          )}

          <details className="text-sm">
            <summary className="text-xs text-zinc-500 cursor-pointer">
              {t("composer.advanced")}
            </summary>
            <div className="mt-2 space-y-2">
              <div>
                <label className="text-xs text-zinc-500">
                  {t("composer.negative_prompt_label")}
                </label>
                <input
                  value={iterNegative}
                  onChange={(e) => setIterNegative(e.target.value)}
                  className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                  placeholder={t("composer.negative_prompt_placeholder")}
                />
              </div>
              <div>
                <label className="text-xs text-zinc-500">
                  {t("composer.seed_label")}
                </label>
                <input
                  type="number"
                  value={iterSeed}
                  onChange={(e) => setIterSeed(e.target.value)}
                  className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                />
              </div>
              {workflowParamsEditor()}
            </div>
          </details>

          <button
            disabled={busy || !iterPrompt.trim()}
            onClick={() => run(submitIterate)}
            className="w-full px-3 py-2 rounded bg-sky-600 hover:bg-sky-500 text-white text-sm disabled:opacity-50"
          >
            {t("detail.iterate_submit")}
          </button>
        </div>
      )}
    </div>
  );
}
