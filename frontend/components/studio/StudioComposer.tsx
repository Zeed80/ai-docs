"use client";

import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";

import { isNative, pickImage } from "@/lib/native-bridge";
import {
  GenerateInput,
  Operation,
  Workflow,
  generate,
  listWorkflows,
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

  // Custom (e.g. trained-LoRA) workflows for the current operation; empty
  // value = the builtin one, so nothing changes for users without customs.
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflowId, setWorkflowId] = useState<string>("");
  // HD tiled cleanup — maximum quality, minutes per sheet.
  const [hd, setHd] = useState(false);
  // High-quality model upscale of the result (any mode): 1 = off, 2/3/4×.
  const [upscale, setUpscale] = useState(1);
  // Post-processing after ComfyUI (cleanup/edit). "auto" = let the workflow
  // decide (LoRA-cleanup keeps its tuned pass); "none" = raw ComfyUI result;
  // "text_only"/"full" opt into enhancements. Default "auto".
  const [postprocess, setPostprocess] = useState("auto");
  useEffect(() => {
    listWorkflows()
      .then((ws) => setWorkflows(ws.filter((w) => !w.is_builtin && w.enabled)))
      .catch(() => undefined);
  }, []);
  const customWorkflows = workflows.filter((w) => w.operation === operation);
  // Default to the newest custom (trained-LoRA) workflow for the operation:
  // the user expects "очистка" to mean the best available pipeline — running
  // the builtin because a selector was overlooked wastes the trained LoRA
  // (confirmed live: 4 of 5 user runs went without it).
  useEffect(() => {
    const customs = workflows.filter((w) => w.operation === operation);
    setWorkflowId(customs.length ? customs[0].id : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [operation, workflows.length]);

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

  const op = OPERATIONS.find((o) => o.key === operation)!;

  async function submitTech() {
    if (!techDesc.trim()) {
      setErr(t("tech_error_empty"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await techDraw(techDesc, techView, link);
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
        params: { seed: Number(seed) || 0, quality },
        source_image_paths: [],
        ...link,
      };
      if (workflowId) {
        input.workflow_id = workflowId;
        // Custom LoRA workflows carry their own tuned steps/cfg (e.g.
        // cleanup-LoRA works at cfg=1.0/25 steps) — the fast/quality preset
        // must not override them.
        delete (input.params as Record<string, unknown>).quality;
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
          <p className="text-xs text-zinc-500">{t("tech_hint")}</p>
          <textarea
            value={techDesc}
            onChange={(e) => setTechDesc(e.target.value)}
            rows={5}
            placeholder={t("tech_placeholder")}
            className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
          />
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

          {customWorkflows.length > 0 && (
            <label className="block text-xs text-zinc-400">
              {t("workflow_label")}
              <select
                value={workflowId}
                onChange={(e) => setWorkflowId(e.target.value)}
                className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white mt-1"
              >
                <option value="">{t("workflow_builtin")}</option>
                {customWorkflows.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.title}
                  </option>
                ))}
              </select>
            </label>
          )}

          {/* Speed/quality tradeoff: measured live, the fast preset
              (Lightning LoRA, 4 steps) never once performed a real edit
              instruction across 6+ test runs — quality mode did roughly
              half the time (diffusion sampling isn't fully seed-
              deterministic here), at several times the generation time.
              Hidden for custom (LoRA) workflows: they carry their own tuned
              steps/cfg and the preset would sabotage them. */}
          <div className={workflowId ? "hidden" : undefined}>
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
