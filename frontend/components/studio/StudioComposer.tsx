"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { isNative, pickImage } from "@/lib/native-bridge";
import {
  GenerateInput,
  Operation,
  Workflow,
  duplicateWorkflow,
  generate,
  listWorkflows,
  patchWorkflow,
  promptHelp,
  techDraw,
  uploadSource,
} from "@/lib/studio-api";

import MaskCanvas, { MaskCanvasHandle } from "./MaskCanvas";

const OPERATIONS: { key: Operation; labelKey: string; needsSource: boolean }[] =
  [
    { key: "edit", labelKey: "op_edit", needsSource: true },
    { key: "generate", labelKey: "op_generate", needsSource: false },
    { key: "inpaint", labelKey: "op_inpaint", needsSource: true },
    { key: "cleanup", labelKey: "op_cleanup", needsSource: true },
  ];

// Output size presets for text→image modes (generate / eskd-diffusion). All
// dims are multiples of 16 (EmptyFlux2LatentImage/EmptySD3LatentImage step).
const SIZE_PRESETS: { labelKey: string; w: number; h: number }[] = [
  { labelKey: "size_square", w: 1024, h: 1024 },
  { labelKey: "size_a4_portrait", w: 896, h: 1280 },
  { labelKey: "size_a4_landscape", w: 1280, h: 896 },
  { labelKey: "size_3_2", w: 1216, h: 832 },
  { labelKey: "size_16_9", w: 1344, h: 768 },
];

interface Props {
  onSubmitted: () => void;
}

export default function StudioComposer({ onSubmitted }: Props) {
  const t = useTranslations("studio.composer");
  const [operation, setOperation] = useState<Operation>("edit");
  const [prompt, setPrompt] = useState("");
  const [negative, setNegative] = useState("");
  const [seed, setSeed] = useState<string>("0");
  const [quality, setQuality] = useState<"fast" | "quality">("fast");
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [sourcePreview, setSourcePreview] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [helping, setHelping] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const maskRef = useRef<MaskCanvasHandle>(null);

  // Exact technical drawing (deterministic ЕСКД render, not diffusion).
  const [techMode, setTechMode] = useState(false);
  const [techDesc, setTechDesc] = useState("");
  const [techView, setTechView] = useState<"front" | "isometric">("front");

  // Traceability: attach the result to a document/case (optional).
  const [linkDocId, setLinkDocId] = useState("");
  const [linkCaseId, setLinkCaseId] = useState("");
  const link = {
    source_document_id: linkDocId.trim() || undefined,
    case_id: linkCaseId.trim() || undefined,
  };

  // Every mode now exposes a workflow (pipeline) selector. We load ALL enabled
  // workflows once and filter by the active operation, so builtins are
  // selectable too — e.g. the ControlNet edit variant that was previously
  // unreachable. ЕСКД mode maps to operation "eskd" plus a sentinel entry ("")
  // that means the deterministic vector render.
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflowId, setWorkflowId] = useState<string>("");
  // Inline rename after a quick "make my own copy".
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  // HD tiled cleanup — maximum quality, minutes per sheet.
  const [hd, setHd] = useState(false);
  // High-quality model upscale of the result (any mode): 1 = off, 2/3/4×.
  const [upscale, setUpscale] = useState(1);
  // Post-processing after ComfyUI (cleanup/edit). "auto" = let the workflow
  // decide (LoRA-cleanup keeps its tuned pass); "none" = raw ComfyUI result;
  // "text_only"/"full" opt into enhancements. Default "auto".
  const [postprocess, setPostprocess] = useState("auto");
  // Output size for text→image modes: index into SIZE_PRESETS, or -1 = custom.
  const [sizePreset, setSizePreset] = useState(0);
  const [customW, setCustomW] = useState("1024");
  const [customH, setCustomH] = useState("1024");

  // Active operation the selector filters on: the diffusion op, or "eskd" in
  // the exact-drawing mode.
  const activeOp: Operation = techMode ? "eskd" : operation;
  const optsForOp = workflows.filter((w) => w.operation === activeOp);
  const selectedWorkflow = workflows.find((w) => w.id === workflowId) ?? null;
  // The fast/quality preset (Lightning-4steps LoRA ↔ full quality) is only
  // meaningful for workflows that actually have a Lightning LoRA node — i.e.
  // the Qwen-Image-Edit builtins whose inject_map exposes lora_strength. FLUX.2
  // builtins and custom LoRA clones carry their own tuned steps, so hide it.
  const showQuality =
    !!selectedWorkflow &&
    selectedWorkflow.is_builtin &&
    !!(selectedWorkflow.inject_map as Record<string, unknown>)?.lora_strength;

  function effectiveSize(): { width: number; height: number } {
    const preset = SIZE_PRESETS[sizePreset];
    if (preset) return { width: preset.w, height: preset.h };
    const clamp = (n: number) =>
      Math.min(2048, Math.max(256, Math.round((Number(n) || 1024) / 16) * 16));
    return { width: clamp(Number(customW)), height: clamp(Number(customH)) };
  }

  async function reloadWorkflows(): Promise<Workflow[]> {
    const ws = (await listWorkflows()).filter((w) => w.enabled);
    setWorkflows(ws);
    return ws;
  }

  useEffect(() => {
    reloadWorkflows().catch(() => undefined);
  }, []);

  // Default selection when the mode (or the loaded list) changes:
  //  • ЕСКД → the deterministic vector render ("") is the recommended default;
  //  • diffusion modes → the newest custom pipeline if any (users expect
  //    "очистка" to mean the best available pipeline — running the builtin
  //    because a selector was overlooked wastes a trained LoRA), else the
  //    first builtin for the operation.
  useEffect(() => {
    setRenaming(null);
    if (techMode) {
      setWorkflowId("");
      return;
    }
    const opts = workflows.filter((w) => w.operation === operation);
    const custom = opts.find((w) => !w.is_builtin);
    setWorkflowId(custom ? custom.id : (opts[0]?.id ?? ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [operation, techMode, workflows.length]);

  const op = OPERATIONS.find((o) => o.key === operation)!;

  /** Quick "make my own copy": duplicate the selected builtin (or the first
   * builtin for this operation) — the copy inherits a correct inject_map, so
   * it runs immediately. Then offer inline rename. For deeper changes (import
   * a ComfyUI JSON, remap nodes) users go to the Workflows tab. */
  async function addOwnWorkflow() {
    setErr(null);
    const base =
      (selectedWorkflow && selectedWorkflow.is_builtin && selectedWorkflow) ||
      optsForOp.find((w) => w.is_builtin);
    if (!base) {
      setErr(t("workflow_add_need_import"));
      return;
    }
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

  async function submitTech() {
    if (!techDesc.trim()) {
      setErr(t("tech_error_empty"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      if (workflowId) {
        // A diffusion ЕСКД pipeline was chosen instead of the exact vector
        // render — the description acts as the prompt (operation "eskd").
        const { width, height } = effectiveSize();
        const input: GenerateInput = {
          operation: "eskd",
          prompt: techDesc,
          params: { seed: Number(seed) || 0, width, height },
          source_image_paths: [],
          workflow_id: workflowId,
          ...link,
        };
        if (upscale > 1) {
          (input.params as Record<string, unknown>).upscale = upscale;
        }
        await generate(input);
      } else {
        await techDraw(techDesc, techView, link);
      }
      setTechDesc("");
      onSubmitted();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  function setSource(file: File | null) {
    setSourceFile(file);
    if (sourcePreview) URL.revokeObjectURL(sourcePreview);
    setSourcePreview(file ? URL.createObjectURL(file) : null);
  }

  async function pickFromGallery() {
    const files = await pickImage("PHOTOS");
    if (files.length) setSource(files[0]);
  }
  async function pickFromCamera() {
    const files = await pickImage("CAMERA");
    if (files.length) setSource(files[0]);
  }

  async function helpWithPrompt() {
    if (!prompt.trim()) return;
    setHelping(true);
    setErr(null);
    try {
      const res = await promptHelp(prompt, operation);
      if (res.prompt) setPrompt(res.prompt);
      if (res.negative_prompt) setNegative(res.negative_prompt);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setHelping(false);
    }
  }

  async function submit() {
    setErr(null);
    if (op.needsSource && !sourceFile) {
      setErr(t("error_need_source"));
      return;
    }
    if (operation === "generate" && !prompt.trim()) {
      setErr(t("error_need_prompt"));
      return;
    }
    setBusy(true);
    try {
      const input: GenerateInput = {
        operation,
        prompt: prompt || undefined,
        negative_prompt: negative || undefined,
        params: { seed: Number(seed) || 0 },
        source_image_paths: [],
        ...link,
      };
      if (workflowId) {
        input.workflow_id = workflowId;
      }
      // The fast/quality preset only applies to Lightning-LoRA workflows; other
      // builtins (FLUX.2) and custom clones carry their own tuned steps.
      if (showQuality) {
        (input.params as Record<string, unknown>).quality = quality;
      }
      // Output size (text→image only): generate mode picks a format/custom W×H.
      if (operation === "generate") {
        const { width, height } = effectiveSize();
        (input.params as Record<string, unknown>).width = width;
        (input.params as Record<string, unknown>).height = height;
      }
      if (operation === "cleanup" && hd) {
        (input.params as Record<string, unknown>).hd = true;
      }
      if (upscale > 1) {
        (input.params as Record<string, unknown>).upscale = upscale;
      }
      // Only send an explicit postprocess override; "auto" leaves the
      // workflow/backend default in place.
      if (
        postprocess !== "auto" &&
        (operation === "cleanup" || operation === "edit")
      ) {
        (input.params as Record<string, unknown>).postprocess = postprocess;
      }
      if (sourceFile) {
        input.source_image_paths = [await uploadSource(sourceFile, "source")];
      }
      if (operation === "inpaint" && maskRef.current) {
        const blob = await maskRef.current.getMaskBlob();
        if (!blob) {
          setErr(t("error_need_mask"));
          setBusy(false);
          return;
        }
        const maskFile = new File([blob], "mask.png", { type: "image/png" });
        input.mask_path = await uploadSource(maskFile, "mask");
      }
      await generate(input);
      setPrompt("");
      setNegative("");
      onSubmitted();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  /** Output-size picker for text→image modes: standard formats + custom W×H. */
  function sizePicker() {
    const custom = sizePreset < 0;
    return (
      <div>
        <div className="mb-1 text-xs text-zinc-500">{t("size_label")}</div>
        <div className="flex flex-wrap gap-1">
          {SIZE_PRESETS.map((p, i) => (
            <button
              key={p.labelKey}
              onClick={() => setSizePreset(i)}
              className={`px-2 py-1 rounded text-xs ${
                sizePreset === i
                  ? "bg-sky-600 text-white"
                  : "bg-white/5 text-zinc-300 hover:bg-white/10"
              }`}
            >
              {t(p.labelKey)} · {p.w}×{p.h}
            </button>
          ))}
          <button
            onClick={() => setSizePreset(-1)}
            className={`px-2 py-1 rounded text-xs ${
              custom
                ? "bg-sky-600 text-white"
                : "bg-white/5 text-zinc-300 hover:bg-white/10"
            }`}
          >
            {t("size_custom")}
          </button>
        </div>
        {custom && (
          <div className="mt-2 flex items-center gap-2">
            <input
              type="number"
              step={16}
              value={customW}
              onChange={(e) => setCustomW(e.target.value)}
              placeholder={t("size_w")}
              className="w-24 rounded bg-zinc-900 border border-white/10 p-1.5 text-sm text-zinc-200"
            />
            <span className="text-zinc-500">×</span>
            <input
              type="number"
              step={16}
              value={customH}
              onChange={(e) => setCustomH(e.target.value)}
              placeholder={t("size_h")}
              className="w-24 rounded bg-zinc-900 border border-white/10 p-1.5 text-sm text-zinc-200"
            />
            <span className="text-[11px] text-zinc-600">{t("size_hint")}</span>
          </div>
        )}
      </div>
    );
  }

  /** Workflow (pipeline) picker shared by every mode. `withSentinel` adds the
   * deterministic-vector default at the top (ЕСКД mode only). */
  function workflowSelector(withSentinel: boolean) {
    const hasOptions = withSentinel || optsForOp.length > 0;
    if (!hasOptions) return null;
    return (
      <div>
        <div className="flex items-center justify-between mb-1">
          <label className="text-xs text-zinc-500">{t("workflow_label")}</label>
          <button
            type="button"
            onClick={addOwnWorkflow}
            className="text-xs text-sky-400 hover:text-sky-300"
          >
            {t("workflow_add_own")}
          </button>
        </div>
        <select
          value={workflowId}
          onChange={(e) => setWorkflowId(e.target.value)}
          className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white"
        >
          {withSentinel && <option value="">{t("eskd_vector_default")}</option>}
          {optsForOp.map((w) => (
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
              placeholder={t("workflow_rename_placeholder")}
              className="flex-1 rounded bg-zinc-900 border border-white/10 px-2 py-1 text-sm text-zinc-200"
            />
            <button
              onClick={saveRename}
              className="px-2 py-1 rounded bg-sky-600 hover:bg-sky-500 text-white text-xs"
            >
              {t("workflow_rename_save")}
            </button>
            <button
              onClick={() => setRenaming(null)}
              className="px-2 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
            >
              {t("workflow_rename_cancel")}
            </button>
          </div>
        )}
        {selectedWorkflow?.description && renaming !== workflowId && (
          <p className="mt-1 text-[11px] text-zinc-600">
            {selectedWorkflow.description}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Traceability: optional link to a document/case (shared by both modes) */}
      <details className="text-sm">
        <summary className="text-xs text-zinc-500 cursor-pointer">
          {t("link_summary")}
        </summary>
        <div className="mt-2 grid grid-cols-2 gap-2">
          <input
            value={linkDocId}
            onChange={(e) => setLinkDocId(e.target.value)}
            placeholder={t("link_doc_placeholder")}
            className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
          />
          <input
            value={linkCaseId}
            onChange={(e) => setLinkCaseId(e.target.value)}
            placeholder={t("link_case_placeholder")}
            className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
          />
        </div>
      </details>

      {/* Mode: diffusion (image) vs exact technical drawing (ЕСКД) */}
      <div className="grid grid-cols-2 gap-1 p-1 rounded bg-white/5">
        <button
          onClick={() => setTechMode(false)}
          className={`px-3 py-1.5 rounded text-sm ${!techMode ? "bg-sky-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
        >
          {t("mode_image")}
        </button>
        <button
          onClick={() => setTechMode(true)}
          className={`px-3 py-1.5 rounded text-sm ${techMode ? "bg-emerald-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
        >
          {t("mode_techdraw")}
        </button>
      </div>

      {techMode && (
        <div className="space-y-3">
          {/* ЕСКД pipeline: exact vector (default) or a diffusion ЕСКД workflow */}
          {workflowSelector(true)}
          <p className="text-xs text-zinc-500">
            {workflowId ? t("eskd_diffusion_hint") : t("tech_hint")}
          </p>
          <textarea
            value={techDesc}
            onChange={(e) => setTechDesc(e.target.value)}
            rows={5}
            placeholder={t("tech_placeholder")}
            className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
          />
          {/* Size applies to a diffusion ЕСКД pipeline (text→image). */}
          {workflowId && sizePicker()}
          {/* View is meaningful only for the deterministic vector render. */}
          {!workflowId && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-zinc-500">
                {t("tech_view_label")}
              </span>
              {(["front", "isometric"] as const).map((v) => (
                <button
                  key={v}
                  onClick={() => setTechView(v)}
                  className={`px-3 py-1 rounded text-sm ${techView === v ? "bg-emerald-600 text-white" : "bg-white/5 text-zinc-300 hover:bg-white/10"}`}
                >
                  {v === "front" ? t("tech_view_front") : t("tech_view_iso")}
                </button>
              ))}
            </div>
          )}
          {/* Upscale applies to the diffusion ЕСКД result too. */}
          {workflowId && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-zinc-500">
                  {t("upscale_label")}
                </span>
                <span className="text-[11px] text-zinc-600">
                  {upscale > 1 ? t("upscale_hint_on") : t("upscale_hint_off")}
                </span>
              </div>
              <div className="grid grid-cols-4 gap-1 p-1 rounded bg-white/5">
                {[1, 2, 3, 4].map((f) => (
                  <button
                    key={f}
                    onClick={() => setUpscale(f)}
                    className={`px-2 py-1.5 rounded text-sm ${
                      upscale === f
                        ? "bg-sky-600 text-white"
                        : "text-zinc-300 hover:bg-white/10"
                    }`}
                  >
                    {f === 1 ? t("upscale_off") : `${f}×`}
                  </button>
                ))}
              </div>
            </div>
          )}
          {err && <div className="text-xs text-red-400">{err}</div>}
          <button
            onClick={submitTech}
            disabled={busy}
            className="w-full px-4 py-2.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white font-medium disabled:opacity-50"
          >
            {busy ? t("tech_submit_busy") : t("tech_submit")}
          </button>
        </div>
      )}

      {!techMode && (
        <>
          {/* Operation tabs */}
          <div className="flex flex-wrap gap-1">
            {OPERATIONS.map((o) => (
              <button
                key={o.key}
                onClick={() => setOperation(o.key)}
                className={`px-3 py-1.5 rounded text-sm ${
                  operation === o.key
                    ? "bg-sky-600 text-white"
                    : "bg-white/5 text-zinc-300 hover:bg-white/10"
                }`}
              >
                {t(o.labelKey)}
              </button>
            ))}
          </div>

          {/* Workflow (pipeline) selector — every diffusion mode. */}
          {workflowSelector(false)}

          {/* Output size — creation (text→image) only. */}
          {operation === "generate" && sizePicker()}

          {operation === "cleanup" && (
            <label className="flex items-center gap-2 text-xs text-zinc-400">
              <input
                type="checkbox"
                checked={hd}
                onChange={(e) => setHd(e.target.checked)}
              />
              {t("hd_label")}
            </label>
          )}

          {/* Speed/quality tradeoff: measured live, the fast preset
              (Lightning LoRA, 4 steps) never once performed a real edit
              instruction across 6+ test runs — quality mode did roughly
              half the time (diffusion sampling isn't fully seed-
              deterministic here), at several times the generation time.
              Shown only for Lightning-LoRA builtins (see showQuality); FLUX.2
              builtins and custom clones carry their own tuned steps. */}
          <div className={showQuality ? undefined : "hidden"}>
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs text-zinc-500">
                {t("quality_label")}
              </span>
              <span className="text-[11px] text-zinc-600">
                {quality === "quality"
                  ? t("quality_hint_quality")
                  : t("quality_hint_fast")}
              </span>
            </div>
            <div className="grid grid-cols-2 gap-1 p-1 rounded bg-white/5">
              <button
                onClick={() => setQuality("fast")}
                className={`px-3 py-1.5 rounded text-sm ${quality === "fast" ? "bg-sky-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
              >
                {t("quality_fast")}
              </button>
              <button
                onClick={() => setQuality("quality")}
                className={`px-3 py-1.5 rounded text-sm ${quality === "quality" ? "bg-emerald-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
              >
                {t("quality_quality")}
              </button>
            </div>
          </div>

          {/* High-quality upscale (any mode) */}
          <div>
            <div className="flex items-center justify-between mb-1">
              <span className="text-xs text-zinc-500">
                {t("upscale_label")}
              </span>
              <span className="text-[11px] text-zinc-600">
                {upscale > 1 ? t("upscale_hint_on") : t("upscale_hint_off")}
              </span>
            </div>
            <div className="grid grid-cols-4 gap-1 p-1 rounded bg-white/5">
              {[1, 2, 3, 4].map((f) => (
                <button
                  key={f}
                  onClick={() => setUpscale(f)}
                  className={`px-2 py-1.5 rounded text-sm ${
                    upscale === f
                      ? "bg-sky-600 text-white"
                      : "text-zinc-300 hover:bg-white/10"
                  }`}
                >
                  {f === 1 ? t("upscale_off") : `${f}×`}
                </button>
              ))}
            </div>
          </div>

          {/* Post-processing (cleanup/edit) */}
          {(operation === "cleanup" || operation === "edit") && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs text-zinc-500">
                  {t("postprocess_label")}
                </span>
                <span className="text-[11px] text-zinc-600">
                  {t("postprocess_hint")}
                </span>
              </div>
              <div className="grid grid-cols-4 gap-1 p-1 rounded bg-white/5">
                {(
                  [
                    ["auto", t("postprocess_auto")],
                    ["none", t("postprocess_none")],
                    ["text_only", t("postprocess_light")],
                    ["full", t("postprocess_full")],
                  ] as const
                ).map(([val, label]) => (
                  <button
                    key={val}
                    onClick={() => setPostprocess(val)}
                    className={`px-2 py-1.5 rounded text-xs ${
                      postprocess === val
                        ? "bg-sky-600 text-white"
                        : "text-zinc-300 hover:bg-white/10"
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Source image */}
          {op.needsSource && (
            <div>
              <div className="flex flex-wrap gap-2 mb-2">
                <label className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm cursor-pointer">
                  {t("pick_file")}
                  <input
                    type="file"
                    accept="image/*"
                    className="hidden"
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) setSource(f);
                    }}
                  />
                </label>
                {isNative() && (
                  <>
                    <button
                      onClick={pickFromCamera}
                      className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
                    >
                      {t("take_photo")}
                    </button>
                    <button
                      onClick={pickFromGallery}
                      className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
                    >
                      {t("from_gallery")}
                    </button>
                  </>
                )}
                {sourceFile && (
                  <button
                    onClick={() => setSource(null)}
                    className="px-3 py-1.5 rounded bg-white/5 hover:bg-white/10 text-sm text-zinc-400"
                  >
                    {t("remove_source")}
                  </button>
                )}
              </div>
              {sourcePreview &&
                (operation === "inpaint" ? (
                  <MaskCanvas ref={maskRef} imageUrl={sourcePreview} />
                ) : (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={sourcePreview}
                    alt={t("source_alt")}
                    className="max-h-64 rounded border border-white/10"
                  />
                ))}
            </div>
          )}

          {/* Prompt (not needed for pure cleanup) */}
          {operation !== "cleanup" && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-zinc-500">
                  {operation === "generate"
                    ? t("prompt_label_generate")
                    : t("prompt_label_edit")}
                </label>
                <button
                  onClick={helpWithPrompt}
                  disabled={helping || !prompt.trim()}
                  className="text-xs text-sky-400 hover:text-sky-300 disabled:opacity-40"
                >
                  {helping ? t("help_prompt_busy") : t("help_prompt")}
                </button>
              </div>
              <textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={3}
                placeholder={
                  operation === "generate"
                    ? t("placeholder_generate")
                    : t("placeholder_edit")
                }
                className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
              />
            </div>
          )}

          <details className="text-sm">
            <summary className="text-xs text-zinc-500 cursor-pointer">
              {t("advanced")}
            </summary>
            <div className="mt-2 space-y-2">
              {operation !== "cleanup" && (
                <div>
                  <label className="text-xs text-zinc-500">
                    {t("negative_prompt_label")}
                  </label>
                  <input
                    value={negative}
                    onChange={(e) => setNegative(e.target.value)}
                    className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                    placeholder={t("negative_prompt_placeholder")}
                  />
                </div>
              )}
              <div>
                <label className="text-xs text-zinc-500">
                  {t("seed_label")}
                </label>
                <input
                  type="number"
                  value={seed}
                  onChange={(e) => setSeed(e.target.value)}
                  className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                />
              </div>
            </div>
          </details>

          {err && <div className="text-xs text-red-400">{err}</div>}

          <button
            onClick={submit}
            disabled={busy}
            className="w-full px-4 py-2.5 rounded bg-sky-600 hover:bg-sky-500 text-white font-medium disabled:opacity-50"
          >
            {busy ? t("submit_busy") : t("submit")}
          </button>
        </>
      )}
    </div>
  );
}
