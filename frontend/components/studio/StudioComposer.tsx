"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { isNative, pickImage } from "@/lib/native-bridge";
import {
  GenerateInput,
  Generation,
  Operation,
  TechDrawView,
  Workflow,
  createBlankSheet,
  duplicateWorkflow,
  generate,
  listWorkflows,
  patchWorkflow,
  promptHelp,
  resultUrl,
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
  generatedSources?: Generation[];
}

type StudioComposerPrefs = {
  operation?: Operation;
  techMode?: boolean;
  mode?: "image" | "tech" | "vector";
  vectorScale?: string;
  vectorSheetFormat?: "" | "A4" | "A3" | "A2" | "A1" | "A0";
  vectorMethod?: "trace" | "spec" | "text_spec";
  vectorDescription?: string;
  vectorLandscape?: boolean;
  blankFormat?: "A4" | "A3" | "A2" | "A1";
  blankLandscape?: boolean;
  prompt?: string;
  negative?: string;
  cleanupPrompt?: string;
  cleanupPromptVisible?: boolean;
  seed?: string;
  quality?: "fast" | "quality";
  techDesc?: string;
  techView?: TechDrawView;
  linkDocId?: string;
  linkCaseId?: string;
  workflowId?: string;
  workflowByOperation?: Partial<Record<Operation, string>>;
  workflowParamOverrides?: Record<string, Record<string, unknown>>;
  hd?: boolean;
  upscale?: number;
  postprocess?: string;
  sizePreset?: number;
  customW?: string;
  customH?: string;
};

const PREFS_KEY = "ai-docs:studio-composer:v2";
const ESKD_STYLE_SUFFIX =
  ", технический чертёж по ЕСКД: чёрно-белая линейная графика на белом фоне, " +
  "сплошные основные линии контура, тонкие сплошные линии для размеров, " +
  "штрихпунктирные осевые и центровые линии, штриховка сечений под 45°, " +
  "без рамки листа, без углового штампа, без основной надписи, без таблицы";
const ESKD_STYLE_MARKER = "технический чертёж по ЕСКД";

function generationLabel(g: Generation): string {
  return g.prompt?.trim() || g.operation;
}

function readPrefs(): StudioComposerPrefs {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(PREFS_KEY);
    return raw ? (JSON.parse(raw) as StudioComposerPrefs) : {};
  } catch {
    return {};
  }
}

export default function StudioComposer({
  onSubmitted,
  generatedSources = [],
}: Props) {
  const t = useTranslations("studio.composer");
  const prefsRef = useRef<StudioComposerPrefs | null>(null);
  if (prefsRef.current === null) prefsRef.current = readPrefs();
  const prefs = prefsRef.current;
  const [operation, setOperation] = useState<Operation>(
    prefs.operation ?? "edit",
  );
  const [prompt, setPrompt] = useState(prefs.prompt ?? "");
  const [negative, setNegative] = useState(prefs.negative ?? "");
  const [cleanupPrompt, setCleanupPrompt] = useState(prefs.cleanupPrompt ?? "");
  const [cleanupPromptVisible, setCleanupPromptVisible] = useState(
    Boolean(prefs.cleanupPromptVisible),
  );
  const [seed, setSeed] = useState<string>(prefs.seed ?? "0");
  const [quality, setQuality] = useState<"fast" | "quality">(
    prefs.quality ?? "fast",
  );
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [sourcePreview, setSourcePreview] = useState<string | null>(null);
  const [sourceGenerationId, setSourceGenerationId] = useState<string>("");
  const [sourcePickerOpen, setSourcePickerOpen] = useState(false);
  const [previewGenerationId, setPreviewGenerationId] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [helping, setHelping] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const maskRef = useRef<MaskCanvasHandle>(null);

  // Composer mode: diffusion image, exact ЕСКД render, or scan→DXF digitizing.
  // (prefs.techMode is the legacy boolean — migrate it into `mode` once.)
  const [mode, setMode] = useState<"image" | "tech" | "vector">(
    prefs.mode ?? (prefs.techMode ? "tech" : "image"),
  );
  const techMode = mode === "tech";
  const [techDesc, setTechDesc] = useState(prefs.techDesc ?? "");
  const [techView, setTechView] = useState<TechDrawView>(
    prefs.techView ?? "front",
  );
  // Vector (digitize) mode: optional manual scale + blank-sheet drafting.
  const [vectorScale, setVectorScale] = useState(prefs.vectorScale ?? "");
  const [vectorSheetFormat, setVectorSheetFormat] = useState<
    "" | "A4" | "A3" | "A2" | "A1" | "A0"
  >(prefs.vectorSheetFormat ?? "");
  // "spec" is graph-first sheet recognition; "text_spec" is the separate
  // auxiliary text-to-parametric-drawing workflow.
  const [vectorMethod, setVectorMethod] = useState<
    "trace" | "spec" | "text_spec"
  >(
    prefs.vectorMethod ?? "trace",
  );
  const [vectorDescription, setVectorDescription] = useState(
    prefs.vectorDescription ?? "",
  );
  const [vectorLandscape, setVectorLandscape] = useState(
    prefs.vectorLandscape ?? true,
  );
  const [blankFormat, setBlankFormat] = useState<"A4" | "A3" | "A2" | "A1">(
    prefs.blankFormat ?? "A4",
  );
  const [blankLandscape, setBlankLandscape] = useState(
    Boolean(prefs.blankLandscape),
  );
  const [blankWithFrame, setBlankWithFrame] = useState(false);
  const [blankDesignation, setBlankDesignation] = useState("");
  // Ф4.1/Ф4.3 VLM enrichment (dimension/line hypotheses) — opt-in per run,
  // not persisted: it's an extra LLM call per uncertain crop, the user
  // should decide each time whether the latency/cost is worth it for this
  // particular scan, not have it silently on by default.
  const [vlmEnrich, setVlmEnrich] = useState(false);

  // Traceability: attach the result to a document/case (optional).
  const [linkDocId, setLinkDocId] = useState(prefs.linkDocId ?? "");
  const [linkCaseId, setLinkCaseId] = useState(prefs.linkCaseId ?? "");
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
  const [workflowId, setWorkflowId] = useState<string>(prefs.workflowId ?? "");
  const [workflowByOperation, setWorkflowByOperation] = useState<
    Partial<Record<Operation, string>>
  >(prefs.workflowByOperation ?? {});
  const [workflowParamOverrides, setWorkflowParamOverrides] = useState<
    Record<string, Record<string, unknown>>
  >(prefs.workflowParamOverrides ?? {});
  const [paramSaveMsg, setParamSaveMsg] = useState<string | null>(null);
  // Inline rename after a quick "make my own copy".
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");
  // HD tiled cleanup — maximum quality, minutes per sheet.
  const [hd, setHd] = useState(Boolean(prefs.hd));
  // High-quality model upscale of the result (any mode): 1 = off, 2/3/4×.
  const [upscale, setUpscale] = useState(prefs.upscale ?? 1);
  // Post-processing after ComfyUI (cleanup/edit). "auto" = let the workflow
  // decide (LoRA-cleanup keeps its tuned pass); "none" = raw ComfyUI result;
  // "text_only"/"full" opt into enhancements. Default "auto".
  const [postprocess, setPostprocess] = useState(prefs.postprocess ?? "auto");
  // Output size for text→image modes: index into SIZE_PRESETS, or -1 = custom.
  const [sizePreset, setSizePreset] = useState(prefs.sizePreset ?? 0);
  const [customW, setCustomW] = useState(prefs.customW ?? "1024");
  const [customH, setCustomH] = useState(prefs.customH ?? "1024");

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
  const previewGeneration =
    generatedSources.find((g) => g.id === previewGenerationId) ??
    generatedSources.find((g) => g.id === sourceGenerationId) ??
    generatedSources[0] ??
    null;
  const selectedGeneration =
    generatedSources.find((g) => g.id === sourceGenerationId) ?? null;

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

  useEffect(() => {
    try {
      window.localStorage.setItem(
        PREFS_KEY,
        JSON.stringify({
          operation,
          techMode,
          mode,
          vectorScale,
          vectorSheetFormat,
          vectorMethod,
          vectorDescription,
          vectorLandscape,
          blankFormat,
          blankLandscape,
          prompt,
          negative,
          cleanupPrompt,
          cleanupPromptVisible,
          seed,
          quality,
          techDesc,
          techView,
          linkDocId,
          linkCaseId,
          workflowId,
          workflowByOperation,
          workflowParamOverrides,
          hd,
          upscale,
          postprocess,
          sizePreset,
          customW,
          customH,
        } satisfies StudioComposerPrefs),
      );
    } catch {
      /* localStorage may be unavailable in private/browser-restricted contexts. */
    }
  }, [
    operation,
    techMode,
    mode,
    vectorScale,
    vectorSheetFormat,
    vectorMethod,
    vectorDescription,
    vectorLandscape,
    blankFormat,
    blankLandscape,
    prompt,
    negative,
    cleanupPrompt,
    cleanupPromptVisible,
    seed,
    quality,
    techDesc,
    techView,
    linkDocId,
    linkCaseId,
    workflowId,
    workflowByOperation,
    workflowParamOverrides,
    hd,
    upscale,
    postprocess,
    sizePreset,
    customW,
    customH,
  ]);

  // Default selection when the mode (or the loaded list) changes:
  //  • ЕСКД → the deterministic vector render ("") is the recommended default;
  //  • diffusion modes → the newest custom pipeline if any (users expect
  //    "очистка" to mean the best available pipeline — running the builtin
  //    because a selector was overlooked wastes a trained LoRA), else the
  //    first builtin for the operation.
  useEffect(() => {
    setRenaming(null);
    if (techMode) {
      const saved = workflowByOperation.eskd;
      setWorkflowId(
        saved && workflows.some((w) => w.id === saved && w.operation === "eskd")
          ? saved
          : "",
      );
      return;
    }
    const opts = workflows.filter((w) => w.operation === operation);
    const saved = workflowByOperation[operation];
    if (saved && opts.some((w) => w.id === saved)) {
      setWorkflowId(saved);
      return;
    }
    const custom = opts.find((w) => !w.is_builtin);
    const next = custom ? custom.id : (opts[0]?.id ?? "");
    setWorkflowId(next);
    if (next) setWorkflowByOperation((cur) => ({ ...cur, [operation]: next }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [operation, techMode, workflows.length]);

  const op = OPERATIONS.find((o) => o.key === operation)!;
  const selectedParamsSchema = (selectedWorkflow?.params_schema ||
    {}) as Record<string, Record<string, unknown>>;
  const workflowParamEntries = Object.entries(selectedParamsSchema).filter(
    ([, spec]) => spec && typeof spec === "object",
  );

  function applyEskdPromptPreview(value: string): string {
    const base = value.trim();
    if (base.includes(ESKD_STYLE_MARKER)) return base;
    return base
      ? `${base}${ESKD_STYLE_SUFFIX}`
      : ESKD_STYLE_SUFFIX.replace(/^, /, "");
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

  function currentPromptPreview(): string {
    if (techMode) {
      return workflowId ? applyEskdPromptPreview(techDesc) : techDesc.trim();
    }
    if (operation === "cleanup") {
      return (
        cleanupPrompt.trim() ||
        workflowTextValue("prompt") ||
        t("cleanup_prompt_default")
      );
    }
    if (operation === "generate") return applyEskdPromptPreview(prompt);
    return prompt.trim();
  }

  function showGenerationPrompt() {
    const next = currentPromptPreview();
    if (techMode) {
      setTechDesc(next);
      return;
    }
    if (operation === "cleanup") {
      setCleanupPrompt(next);
      setCleanupPromptVisible(true);
      return;
    }
    setPrompt(next);
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
      setWorkflowByOperation((cur) => ({ ...cur, [activeOp]: updated.id }));
      setWorkflowParamOverrides((cur) => {
        const next = { ...cur };
        delete next[selectedWorkflow.id];
        delete next[updated.id];
        return next;
      });
      setParamSaveMsg(
        selectedWorkflow.is_builtin
          ? t("workflow_params_saved_copy")
          : t("workflow_params_saved"),
      );
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

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
        Object.assign(
          input.params as Record<string, unknown>,
          collectWorkflowParams(),
        );
        await generate(input);
      } else {
        await techDraw(techDesc, techView, link);
      }
      onSubmitted();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function submitVector() {
    const directDescription =
      vectorMethod === "text_spec" ? vectorDescription.trim() : "";
    if (!sourceFile && !sourceGenerationId && !directDescription) {
      setErr(t("error_need_source"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const input: GenerateInput = {
        operation: "vectorize",
        prompt: directDescription || undefined,
        params: {},
        source_image_paths: [],
        ...link,
      };
      (input.params as Record<string, unknown>).vectorize_method = vectorMethod;
      const s = Number(vectorScale.replace(",", "."));
      // The "spec" method auto-picks a ГОСТ 2.302 scale from the sheet +
      // orientation, so a manual scale is ignored there (but the sheet is used).
      if (vectorMethod === "trace" && Number.isFinite(s) && s > 0) {
        (input.params as Record<string, unknown>).scale_mm_per_px = s;
      } else if (vectorSheetFormat) {
        (input.params as Record<string, unknown>).sheet_format =
          vectorSheetFormat;
        if (vectorMethod === "spec" || vectorMethod === "text_spec") {
          (input.params as Record<string, unknown>).sheet_orientation =
            vectorLandscape ? "landscape" : "portrait";
        }
      }
      if (vlmEnrich) {
        (input.params as Record<string, unknown>).vlm_dimensions = true;
        (input.params as Record<string, unknown>).vlm_lines = true;
      }
      if (sourceFile) {
        input.source_image_paths = [await uploadSource(sourceFile, "source")];
      } else if (sourceGenerationId) {
        input.source_image_paths = [`generation:${sourceGenerationId}`];
      }
      await generate(input);
      onSubmitted();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function submitBlankSheet() {
    setBusy(true);
    setErr(null);
    try {
      await createBlankSheet({
        format: blankFormat,
        landscape: blankLandscape,
        case_id: link.case_id,
        with_frame: blankWithFrame,
        designation: blankWithFrame
          ? blankDesignation.trim() || undefined
          : undefined,
      });
      onSubmitted();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  function setSource(file: File | null) {
    setSourceGenerationId("");
    setSourceFile(file);
    if (sourcePreview?.startsWith("blob:")) URL.revokeObjectURL(sourcePreview);
    setSourcePreview(file ? URL.createObjectURL(file) : null);
  }

  function setGeneratedSource(id: string) {
    setSourceGenerationId(id);
    setSourceFile(null);
    if (sourcePreview?.startsWith("blob:")) URL.revokeObjectURL(sourcePreview);
    setSourcePreview(id ? resultUrl(id) : null);
    setPreviewGenerationId(id);
    setSourcePickerOpen(false);
  }

  function clearSource() {
    setSourceGenerationId("");
    setSource(null);
  }

  async function pickFromGallery() {
    const files = await pickImage("PHOTOS");
    if (files.length) setSource(files[0]);
  }
  async function pickFromCamera() {
    const files = await pickImage("CAMERA");
    if (files.length) setSource(files[0]);
  }

  useEffect(() => {
    if (!sourcePickerOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSourcePickerOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [sourcePickerOpen]);

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
    if (op.needsSource && !sourceFile && !sourceGenerationId) {
      setErr(t("error_need_source"));
      return;
    }
    if (
      (operation === "generate" ||
        operation === "edit" ||
        operation === "inpaint") &&
      !prompt.trim()
    ) {
      setErr(t("error_need_prompt"));
      return;
    }
    setBusy(true);
    try {
      const input: GenerateInput = {
        operation,
        prompt:
          operation === "cleanup"
            ? cleanupPrompt.trim() || undefined
            : prompt || undefined,
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
      Object.assign(
        input.params as Record<string, unknown>,
        collectWorkflowParams(),
      );
      if (sourceFile) {
        input.source_image_paths = [await uploadSource(sourceFile, "source")];
      } else if (sourceGenerationId) {
        input.source_image_paths = [`generation:${sourceGenerationId}`];
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
          onChange={(e) => {
            const next = e.target.value;
            setWorkflowId(next);
            setWorkflowByOperation((cur) => ({ ...cur, [activeOp]: next }));
          }}
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

  function workflowParamsEditor() {
    if (!selectedWorkflow || workflowParamEntries.length === 0) return null;
    return (
      <div className="space-y-2 border-t border-white/10 pt-2">
        <div className="flex items-center justify-between gap-2">
          <div>
            <div className="text-xs text-zinc-500">
              {t("workflow_params_label")}
            </div>
            <div className="text-[11px] text-zinc-600">
              {selectedWorkflow.is_builtin
                ? t("workflow_params_builtin_hint")
                : t("workflow_params_hint")}
            </div>
          </div>
          <button
            type="button"
            onClick={saveWorkflowParamDefaults}
            disabled={busy}
            className="shrink-0 rounded bg-white/10 px-2 py-1 text-xs hover:bg-white/20 disabled:opacity-50"
          >
            {t("workflow_params_save")}
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

  function generatedSourcePicker() {
    if (generatedSources.length === 0) return null;
    return (
      <>
        <button
          type="button"
          onClick={() => {
            setPreviewGenerationId(
              sourceGenerationId || generatedSources[0]?.id || "",
            );
            setSourcePickerOpen(true);
          }}
          className="min-w-0 flex-1 rounded bg-white/10 px-3 py-1.5 text-left text-sm text-zinc-200 hover:bg-white/20 sm:flex-none"
          aria-label={t("generated_source_label")}
        >
          {sourceGenerationId
            ? `${t("generated_source_selected")} ${
                selectedGeneration
                  ? generationLabel(selectedGeneration)
                  : sourceGenerationId.slice(0, 8)
              }`
            : t("generated_source_placeholder")}
        </button>

        {sourcePickerOpen && (
          <div
            className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 sm:items-center sm:p-4"
            role="dialog"
            aria-modal="true"
            aria-label={t("generated_source_label")}
            onClick={() => setSourcePickerOpen(false)}
          >
            <div
              className="flex max-h-[92vh] w-full max-w-4xl flex-col overflow-hidden rounded-t-lg border border-white/10 bg-zinc-950 shadow-2xl sm:rounded-lg"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="flex items-center justify-between gap-2 border-b border-white/10 px-3 py-2">
                <div>
                  <div className="text-sm font-medium text-zinc-100">
                    {t("generated_source_label")}
                  </div>
                  {previewGeneration && (
                    <div className="text-[11px] text-zinc-500">
                      {generationLabel(previewGeneration)}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setSourcePickerOpen(false)}
                  className="rounded px-2 py-1 text-sm text-zinc-400 hover:bg-white/10 hover:text-white"
                  aria-label={t("generated_source_close")}
                >
                  ✕
                </button>
              </div>

              <div className="grid min-h-0 flex-1 gap-3 overflow-y-auto p-3 md:grid-cols-[minmax(0,1fr)_260px]">
                <div className="flex min-h-[260px] items-center justify-center rounded border border-white/10 bg-zinc-900">
                  {previewGeneration?.has_result ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={resultUrl(previewGeneration.id)}
                      alt={`${t("gallery.result_alt")} ${generationLabel(previewGeneration)}`}
                      className="max-h-[58vh] w-full object-contain"
                    />
                  ) : (
                    <div className="px-4 text-center text-sm text-zinc-500">
                      {previewGeneration
                        ? t("generated_source_no_result")
                        : t("generated_source_empty")}
                    </div>
                  )}
                </div>

                <div className="grid max-h-56 grid-cols-3 gap-2 overflow-y-auto pr-1 md:max-h-[58vh] md:grid-cols-2">
                  {generatedSources.map((g) => {
                    const active = previewGeneration?.id === g.id;
                    return (
                      <button
                        type="button"
                        key={g.id}
                        onClick={() => setPreviewGenerationId(g.id)}
                        className={`overflow-hidden rounded border text-left ${
                          active
                            ? "border-sky-500 ring-1 ring-sky-500"
                            : "border-white/10 hover:border-white/30"
                        }`}
                      >
                        <div className="aspect-square bg-zinc-900">
                          {g.has_result ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              src={resultUrl(g.id, true)}
                              alt={`${t("gallery.result_alt")} ${generationLabel(g)}`}
                              className="h-full w-full object-cover"
                            />
                          ) : (
                            <div className="flex h-full items-center justify-center text-[11px] text-zinc-500">
                              {t(`status.${g.status}`)}
                            </div>
                          )}
                        </div>
                        <div className="px-1.5 py-1 text-[11px] text-zinc-300">
                          {generationLabel(g)}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>

              <div className="flex items-center justify-between gap-2 border-t border-white/10 px-3 py-2">
                <button
                  type="button"
                  onClick={() => setSourcePickerOpen(false)}
                  className="rounded bg-white/10 px-3 py-1.5 text-sm text-zinc-200 hover:bg-white/20"
                >
                  {t("generated_source_cancel")}
                </button>
                <button
                  type="button"
                  disabled={!previewGeneration?.has_result}
                  onClick={() =>
                    previewGeneration &&
                    setGeneratedSource(previewGeneration.id)
                  }
                  className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-50"
                >
                  {previewGeneration
                    ? `${t("generated_source_use")} ${generationLabel(previewGeneration)}`
                    : t("generated_source_use")}
                </button>
              </div>
            </div>
          </div>
        )}
      </>
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

      {/* Mode: diffusion (image) vs exact ЕСКД render vs scan→DXF digitizing */}
      <div className="grid grid-cols-3 gap-1 p-1 rounded bg-white/5">
        <button
          onClick={() => setMode("image")}
          className={`px-3 py-1.5 rounded text-sm ${mode === "image" ? "bg-sky-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
        >
          {t("mode_image")}
        </button>
        <button
          onClick={() => setMode("tech")}
          className={`px-3 py-1.5 rounded text-sm ${mode === "tech" ? "bg-emerald-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
        >
          {t("mode_techdraw")}
        </button>
        <button
          onClick={() => setMode("vector")}
          className={`px-3 py-1.5 rounded text-sm ${mode === "vector" ? "bg-amber-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
        >
          {t("mode_vector")}
        </button>
      </div>

      {mode === "vector" && (
        <div className="space-y-3">
          <p className="text-xs text-zinc-500">{t("vector_hint")}</p>

          {/* Source: file / camera / previous generation result */}
          <div className="flex flex-wrap items-center gap-2">
            <label className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm cursor-pointer">
              {t("pick_file")}
              <input
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => setSource(e.target.files?.[0] ?? null)}
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
            {generatedSources.length > 0 && (
              <select
                value={sourceGenerationId}
                onChange={(e) => setGeneratedSource(e.target.value)}
                className="rounded bg-zinc-900 border border-white/10 px-2 py-1.5 text-sm text-zinc-200"
              >
                <option value="">{t("generated_source_placeholder")}</option>
                {generatedSources
                  .filter((g) => g.has_result)
                  .map((g) => (
                    <option key={g.id} value={g.id}>
                      {generationLabel(g).slice(0, 60)}
                    </option>
                  ))}
              </select>
            )}
            {(sourceFile || sourceGenerationId) && (
              <button
                onClick={clearSource}
                className="text-xs text-zinc-400 hover:text-white"
              >
                {t("remove_source")}
              </button>
            )}
          </div>
          {sourcePreview && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={sourcePreview}
              alt={t("source_alt")}
              className="max-h-56 rounded border border-white/10 bg-zinc-900"
            />
          )}
          <p className="text-[11px] text-zinc-600">{t("vector_cleanup_tip")}</p>

          {/* Method: pixel tracing vs understanding→drafting (β). */}
          <label className="block">
            <span className="text-xs text-zinc-500">
              {t("vectorize_method")}
            </span>
            <select
              value={vectorMethod}
              onChange={(e) =>
                setVectorMethod(
                  e.target.value as "trace" | "spec" | "text_spec",
                )
              }
              className="mt-1 w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
            >
              <option value="trace">{t("method_trace")}</option>
              <option value="spec">{t("method_spec")}</option>
              <option value="text_spec">{t("method_text_spec")}</option>
            </select>
          </label>

          <div className="grid gap-2 sm:grid-cols-2">
            <label className="block">
              <span className="text-xs text-zinc-500">
                {t("vector_scale_label")}
              </span>
              <input
                value={vectorMethod !== "trace" ? "" : vectorScale}
                disabled={vectorMethod !== "trace"}
                onChange={(e) => setVectorScale(e.target.value)}
                placeholder={
                  vectorMethod !== "trace"
                    ? t("vector_scale_auto")
                    : t("vector_scale_placeholder")
                }
                className="mt-1 w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200 disabled:opacity-50"
              />
            </label>
            <label className="block">
              <span className="text-xs text-zinc-500">
                {t("vector_sheet_format_label")}
              </span>
              <select
                value={vectorSheetFormat}
                disabled={
                  vectorMethod === "trace" && Boolean(vectorScale.trim())
                }
                onChange={(e) =>
                  setVectorSheetFormat(
                    e.target.value as "" | "A4" | "A3" | "A2" | "A1" | "A0",
                  )
                }
                className="mt-1 w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200 disabled:opacity-50"
              >
                <option value="">{t("vector_sheet_format_unknown")}</option>
                {(["A4", "A3", "A2", "A1", "A0"] as const).map((format) => (
                  <option key={format} value={format}>
                    {format}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {vectorMethod === "spec" && (
            <p className="rounded border border-sky-400/20 bg-sky-950/20 px-3 py-2 text-[11px] text-sky-100">
              {t("vector_graph_hint")}
            </p>
          )}

          {/* Free text is a separate auxiliary workflow, not graph recognition. */}
          {vectorMethod === "text_spec" && (
            <div className="space-y-1">
              <label className="block">
                <span className="text-xs text-zinc-500">
                  {t("vector_description_label")}
                </span>
                <textarea
                  value={vectorDescription}
                  onChange={(e) => setVectorDescription(e.target.value)}
                  rows={5}
                  placeholder={t("vector_description_placeholder")}
                  className="mt-1 w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                />
              </label>
              <p className="text-[11px] text-zinc-600">
                {t("vector_description_hint")}
              </p>
              <div className="space-y-1">
                <div className="text-[11px] text-zinc-500">
                  {t("vector_description_templates")}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {(["plate", "flange", "slot"] as const).map((template) => (
                    <button
                      key={template}
                      type="button"
                      onClick={() => setVectorDescription(t(`vector_template_${template}_text`))}
                      className="rounded border border-white/10 bg-white/5 px-2 py-1 text-[11px] text-zinc-300 hover:bg-white/10"
                    >
                      {t(`vector_template_${template}`)}
                    </button>
                  ))}
                </div>
              </div>
              <label className="block">
                <span className="text-xs text-zinc-500">
                  {t("vector_orientation_label")}
                </span>
                <select
                  value={vectorLandscape ? "landscape" : "portrait"}
                  onChange={(e) =>
                    setVectorLandscape(e.target.value === "landscape")
                  }
                  className="mt-1 w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                >
                  <option value="landscape">
                    {t("vector_orientation_landscape")}
                  </option>
                  <option value="portrait">
                    {t("vector_orientation_portrait")}
                  </option>
                </select>
              </label>
              <p className="text-[11px] text-zinc-600">
                {vectorSheetFormat
                  ? t("vector_autoscale_hint")
                  : t("vector_autoscale_need_sheet")}
              </p>
            </div>
          )}

          <label className="flex items-center gap-2 text-xs text-zinc-400">
            <input
              type="checkbox"
              checked={vlmEnrich}
              onChange={(e) => setVlmEnrich(e.target.checked)}
            />
            {t("vector_vlm_enrich_label")}
          </label>

          {err && <div className="text-xs text-red-400">{err}</div>}
          <button
            onClick={submitVector}
            disabled={
              busy ||
              (!sourceFile &&
                !sourceGenerationId &&
                !(vectorMethod === "text_spec" && vectorDescription.trim()))
            }
            className="w-full px-4 py-2.5 rounded bg-amber-600 hover:bg-amber-500 text-white font-medium disabled:opacity-50"
          >
            {busy ? t("vector_submit_busy") : t("vector_submit")}
          </button>

          {/* Manual drafting: an empty ГОСТ sheet to draw on in the editor */}
          <div className="border-t border-white/10 pt-3 space-y-2">
            <div className="text-xs text-zinc-500">
              {t("vector_blank_label")}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={blankFormat}
                onChange={(e) =>
                  setBlankFormat(e.target.value as "A4" | "A3" | "A2" | "A1")
                }
                className="rounded bg-zinc-900 border border-white/10 px-2 py-1.5 text-sm text-zinc-200"
              >
                {(["A4", "A3", "A2", "A1"] as const).map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-1 text-xs text-zinc-400">
                <input
                  type="checkbox"
                  checked={blankLandscape}
                  onChange={(e) => setBlankLandscape(e.target.checked)}
                />
                {t("vector_landscape")}
              </label>
              <label className="flex items-center gap-1 text-xs text-zinc-400">
                <input
                  type="checkbox"
                  checked={blankWithFrame}
                  onChange={(e) => setBlankWithFrame(e.target.checked)}
                />
                {t("vector_blank_with_frame")}
              </label>
              {blankWithFrame && (
                <input
                  value={blankDesignation}
                  onChange={(e) => setBlankDesignation(e.target.value)}
                  placeholder={t("vector_blank_designation_placeholder")}
                  className="rounded bg-zinc-900 border border-white/10 px-2 py-1.5 text-sm text-zinc-200"
                />
              )}
              <button
                onClick={submitBlankSheet}
                disabled={busy}
                className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm disabled:opacity-50"
              >
                {t("vector_blank_create")}
              </button>
            </div>
          </div>
        </div>
      )}

      {techMode && (
        <div className="space-y-3">
          {/* ЕСКД pipeline: exact vector (default) or a diffusion ЕСКД workflow */}
          {workflowSelector(true)}
          <p className="text-xs text-zinc-500">
            {workflowId ? t("eskd_diffusion_hint") : t("tech_hint")}
          </p>
          <div className="flex items-center justify-between gap-2">
            <label className="text-xs text-zinc-500">
              {t("prompt_to_send_label")}
            </label>
            <button
              type="button"
              onClick={showGenerationPrompt}
              className="text-xs text-sky-400 hover:text-sky-300"
            >
              {t("show_prompt")}
            </button>
          </div>
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
            <div className="space-y-1">
              <span className="text-xs text-zinc-500">
                {t("tech_view_label")}
              </span>
              <div className="grid grid-cols-2 gap-1 p-1 rounded bg-white/5 sm:grid-cols-4">
                {(
                  ["front", "section", "half_section", "isometric"] as const
                ).map((v) => (
                  <button
                    key={v}
                    onClick={() => setTechView(v)}
                    className={`px-2 py-1.5 rounded text-sm ${techView === v ? "bg-emerald-600 text-white" : "text-zinc-300 hover:bg-white/10"}`}
                  >
                    {t(`tech_view_${v}`)}
                  </button>
                ))}
              </div>
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
          {workflowId && (
            <details className="text-sm">
              <summary className="text-xs text-zinc-500 cursor-pointer">
                {t("advanced")}
              </summary>
              <div className="mt-2 space-y-2">
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
                {workflowParamsEditor()}
              </div>
            </details>
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

      {mode === "image" && (
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
                {generatedSourcePicker()}
                {(sourceFile || sourceGenerationId) && (
                  <button
                    onClick={clearSource}
                    className="px-3 py-1.5 rounded bg-white/5 hover:bg-white/10 text-sm text-zinc-400"
                  >
                    {t("remove_source")}
                  </button>
                )}
              </div>
              {sourcePreview && (
                <div className="space-y-1">
                  {selectedGeneration && (
                    <div className="text-[11px] text-zinc-500">
                      {generationLabel(selectedGeneration)}
                    </div>
                  )}
                  {operation === "inpaint" ? (
                    <MaskCanvas ref={maskRef} imageUrl={sourcePreview} />
                  ) : (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={sourcePreview}
                      alt={t("source_alt")}
                      className="max-h-64 rounded border border-white/10"
                    />
                  )}
                </div>
              )}
            </div>
          )}

          {/* Main prompt for text generation and instruction-based edits. */}
          {operation !== "cleanup" && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-zinc-500">
                  {operation === "generate"
                    ? t("prompt_label_generate")
                    : t("prompt_label_edit")}
                </label>
                <button
                  type="button"
                  onClick={showGenerationPrompt}
                  className="text-xs text-sky-400 hover:text-sky-300"
                >
                  {t("show_prompt")}
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
              <div className="mt-1 flex justify-end">
                <button
                  onClick={helpWithPrompt}
                  disabled={helping || !prompt.trim()}
                  className="text-xs text-sky-400 hover:text-sky-300 disabled:opacity-40"
                >
                  {helping ? t("help_prompt_busy") : t("help_prompt")}
                </button>
              </div>
            </div>
          )}

          {operation === "cleanup" && (
            <div>
              <div className="flex items-center justify-between mb-1">
                <label className="text-xs text-zinc-500">
                  {t("cleanup_prompt_label")}
                </label>
                <button
                  type="button"
                  onClick={showGenerationPrompt}
                  className="text-xs text-sky-400 hover:text-sky-300"
                >
                  {t("show_prompt")}
                </button>
              </div>
              {cleanupPromptVisible && (
                <textarea
                  value={cleanupPrompt}
                  onChange={(e) => setCleanupPrompt(e.target.value)}
                  rows={3}
                  placeholder={t("cleanup_prompt_placeholder")}
                  className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                />
              )}
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
              {workflowParamsEditor()}
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
