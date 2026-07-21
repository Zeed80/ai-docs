"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import {
  createBlankSheet,
  Generation,
  generate,
  getVectorizerDevelopmentStatus,
  importDxf,
  listGenerations,
  resultUrl,
  updateGenerationMeta,
  uploadSource,
  VectorizerDevelopmentStatus,
} from "@/lib/studio-api";

type SheetFormat = "A4" | "A3" | "A2" | "A1";
type DigitizationProfile =
  | "auto"
  | "mechanical_eskd"
  | "construction"
  | "electrical"
  | "hydraulic"
  | "pid";

function docTitle(g: Generation): string {
  const params = (g.params ?? {}) as Record<string, unknown>;
  return (
    g.prompt ||
    (params.source_filename as string | undefined) ||
    (params.sheet_format ? `Лист ${params.sheet_format}` : "") ||
    g.id.slice(0, 8)
  );
}

function docKind(g: Generation): "blank" | "imported" | "scan" {
  const params = (g.params ?? {}) as Record<string, unknown>;
  if (params.blank) return "blank";
  if (params.imported) return "imported";
  return "scan";
}

export default function CadListPage() {
  const t = useTranslations("cad");
  const router = useRouter();
  const [items, setItems] = useState<Generation[]>([]);
  const [vectorizerStatus, setVectorizerStatus] =
    useState<VectorizerDevelopmentStatus | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showNewSheet, setShowNewSheet] = useState(false);
  const [sheetFormat, setSheetFormat] = useState<SheetFormat>("A4");
  const [landscape, setLandscape] = useState(false);
  const [withFrame, setWithFrame] = useState(true);
  const [sheetTitle, setSheetTitle] = useState("");
  const [digitizationProfile, setDigitizationProfile] =
    useState<DigitizationProfile>("auto");
  const [digitizeSheetFormat, setDigitizeSheetFormat] = useState<
    "" | SheetFormat
  >("");
  const [vectorizeMethod, setVectorizeMethod] = useState<"trace" | "spec">(
    "trace",
  );
  const fileRef = useRef<HTMLInputElement | null>(null);
  const scanRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await listGenerations();
      setItems(data.filter((g) => g.operation === "vectorize"));
      setError(null);
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, []);

  useEffect(() => {
    void load();
    void getVectorizerDevelopmentStatus()
      .then(setVectorizerStatus)
      .catch(() => setVectorizerStatus(null));
    const timer = setInterval(load, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((g) => docTitle(g).toLowerCase().includes(q));
  }, [items, query]);

  const onCreateSheet = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const gen = await createBlankSheet({
        format: sheetFormat,
        landscape,
        with_frame: withFrame,
        title: sheetTitle.trim() || undefined,
      });
      router.push(`/cad/${gen.id}`);
    } catch (e) {
      setError(String((e as Error).message || e));
      setBusy(false);
    }
  }, [landscape, router, sheetFormat, sheetTitle, withFrame]);

  const onImport = useCallback(
    async (file: File) => {
      setBusy(true);
      setError(null);
      try {
        const gen = await importDxf(file);
        router.push(`/cad/${gen.id}`);
      } catch (e) {
        setError(String((e as Error).message || e));
        setBusy(false);
      }
    },
    [router],
  );

  const onDigitize = useCallback(
    async (file: File) => {
      // The main entry point for «оцифруй detal_126.png»: upload the scan/
      // photo and launch vectorize; the editor page polls until it lands.
      setBusy(true);
      setError(null);
      try {
        let pdfPage = 0;
        if (
          file.type === "application/pdf" ||
          file.name.toLowerCase().endsWith(".pdf")
        ) {
          const raw = window.prompt(t("pdf_page_prompt"), "1");
          if (raw === null) {
            setBusy(false);
            return;
          }
          const parsed = Number(raw);
          if (!Number.isInteger(parsed) || parsed < 1) {
            throw new Error(t("pdf_page_invalid"));
          }
          pdfPage = parsed - 1;
        }
        const path = await uploadSource(file);
        const gen = await generate({
          operation: "vectorize",
          prompt: file.name,
          source_image_paths: [path],
          params: {
            quality_mode: "exact_or_refuse",
            digitization_profile: digitizationProfile,
            vectorize_method: vectorizeMethod,
            source_filename: file.name,
            pdf_page: pdfPage,
            pdf_dpi: 300,
            ...(digitizeSheetFormat
              ? { sheet_format: digitizeSheetFormat }
              : {}),
          },
        });
        router.push(`/cad/${gen.id}`);
      } catch (e) {
        setError(String((e as Error).message || e));
        setBusy(false);
      }
    },
    [digitizationProfile, digitizeSheetFormat, vectorizeMethod, router, t],
  );

  const onRename = useCallback(
    async (g: Generation, ev: React.MouseEvent) => {
      // The card is a Link — don't navigate when renaming.
      ev.preventDefault();
      ev.stopPropagation();
      const next = window.prompt(t("rename_prompt"), docTitle(g));
      if (next === null) return;
      try {
        await updateGenerationMeta(g.id, { title: next.trim() });
        await load();
      } catch (e) {
        setError(String((e as Error).message || e));
      }
    },
    [load, t],
  );

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 md:px-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-100">{t("title")}</h1>
          <p className="text-sm text-zinc-400">{t("subtitle")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href="/studio"
            className="rounded border border-white/15 px-3 py-2 text-sm text-zinc-300 hover:bg-white/5"
          >
            {t("open_studio")}
          </Link>
          <select
            value={vectorizeMethod}
            onChange={(e) =>
              setVectorizeMethod(e.target.value as "trace" | "spec")
            }
            className="rounded border border-white/15 bg-zinc-950 px-2 py-2 text-xs text-zinc-200"
            title={t("vectorize_method")}
          >
            <option value="trace">{t("method_trace")}</option>
            <option value="spec">{t("method_spec")}</option>
          </select>
          <select
            value={digitizationProfile}
            onChange={(e) =>
              setDigitizationProfile(e.target.value as DigitizationProfile)
            }
            className="rounded border border-white/15 bg-zinc-950 px-2 py-2 text-xs text-zinc-200"
            title={t("digitization_profile")}
          >
            <option value="auto">{t("profile_auto")}</option>
            <option value="mechanical_eskd">{t("profile_mechanical")}</option>
            <option value="construction">{t("profile_construction")}</option>
            <option value="electrical">{t("profile_electrical")}</option>
            <option value="hydraulic">{t("profile_hydraulic")}</option>
            <option value="pid">{t("profile_pid")}</option>
          </select>
          <select
            value={digitizeSheetFormat}
            onChange={(e) =>
              setDigitizeSheetFormat(e.target.value as "" | SheetFormat)
            }
            className="rounded border border-white/15 bg-zinc-950 px-2 py-2 text-xs text-zinc-200"
            title={t("digitization_sheet_format")}
          >
            <option value="">{t("sheet_format_unknown")}</option>
            {(["A4", "A3", "A2", "A1"] as const).map((format) => (
              <option key={format} value={format}>
                {format}
              </option>
            ))}
          </select>
          <button
            type="button"
            disabled={busy}
            onClick={() => scanRef.current?.click()}
            className="rounded bg-emerald-600 px-3 py-2 text-sm text-white hover:bg-emerald-500 disabled:opacity-50"
          >
            {t("digitize_scan")}
          </button>
          <input
            ref={scanRef}
            type="file"
            accept=".png,.jpg,.jpeg,.webp,.pdf"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void onDigitize(file);
            }}
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => fileRef.current?.click()}
            className="rounded border border-white/15 px-3 py-2 text-sm text-zinc-200 hover:bg-white/5 disabled:opacity-50"
          >
            {t("import_dxf")}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept=".dxf"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void onImport(file);
            }}
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => setShowNewSheet((v) => !v)}
            className="rounded bg-sky-600 px-3 py-2 text-sm text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {t("new_sheet")}
          </button>
        </div>
      </header>

      {vectorizerStatus && (
        <section
          className="rounded-lg border border-amber-400/30 bg-amber-500/10 px-4 py-3"
          aria-label={t("vectorizer_progress_title")}
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-amber-200">
              {t("vectorizer_progress_title")}
            </h2>
            <span className="rounded bg-red-500/20 px-2 py-1 text-xs font-medium text-red-200">
              {t("vectorizer_gate_refused")}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-300">
            {t("vectorizer_progress_summary", {
              models: vectorizerStatus.corpus.projected_models,
              sheets: vectorizerStatus.corpus.exact_sheets,
              tiles: vectorizerStatus.corpus.training_tiles.toLocaleString(),
            })}
          </p>
          <p className="mt-1 text-xs text-zinc-400">
            {t("vectorizer_quality_summary", {
              layoutF1:
                vectorizerStatus.candidate.sheet_layout.view_f1_iou50.toFixed(
                  3,
                ),
              heatmapLineF1:
                vectorizerStatus.candidate.directional_fields.real_holdout_line_f1.toFixed(
                  3,
                ),
              endpointF1:
                vectorizerStatus.candidate.directional_fields.real_holdout_endpoint_f1.toFixed(
                  3,
                ),
              junctionF1:
                vectorizerStatus.candidate.directional_fields.real_holdout_junction_f1.toFixed(
                  3,
                ),
              entityF1:
                vectorizerStatus.candidate.graph_iterations.source_snapped_entity_f1.toFixed(
                  3,
                ),
              exactRate:
                vectorizerStatus.candidate.graph_iterations.source_snapped_exact_sheet_rate.toFixed(
                  3,
                ),
              nativeDxfF1:
                vectorizerStatus.candidate.native_dxf_benchmark.cv_entity_f1.toFixed(
                  3,
                ),
              falseExact:
                vectorizerStatus.candidate.native_dxf_benchmark.cv_false_exact_rate.toFixed(
                  3,
                ),
            })}
          </p>
          <p className="mt-2 rounded border border-red-400/20 bg-red-950/20 px-2 py-1 text-xs text-red-100">
            {t("latest_real_regression", {
              dwg: vectorizerStatus.latest_real_stack_regression.dwg_files,
              photos: vectorizerStatus.latest_real_stack_regression.photo_files,
              precision:
                vectorizerStatus.latest_real_stack_regression.entity_precision.toFixed(4),
              recall:
                vectorizerStatus.latest_real_stack_regression.entity_recall.toFixed(4),
              f1: vectorizerStatus.latest_real_stack_regression.entity_f1.toFixed(4),
              exact:
                vectorizerStatus.latest_real_stack_regression.exact_sheet_rate.toFixed(2),
              falseExact:
                vectorizerStatus.latest_real_stack_regression.false_exact_rate.toFixed(2),
            })}
          </p>
          <p className="mt-2 rounded border border-amber-400/20 bg-amber-950/20 px-2 py-1 text-xs text-amber-100">
            {t("multi_type_candidate_status", {
              step: vectorizerStatus.candidate.multi_type_proposal.checkpoint_step,
              sheets: vectorizerStatus.candidate.multi_type_proposal.independent_holdout_sheets,
              precision: vectorizerStatus.candidate.multi_type_proposal.entity_precision.toFixed(4),
              recall: vectorizerStatus.candidate.multi_type_proposal.entity_recall.toFixed(4),
              f1: vectorizerStatus.candidate.multi_type_proposal.entity_f1.toFixed(4),
              segmentF1: vectorizerStatus.candidate.multi_type_proposal.segment_f1.toFixed(4),
              textF1: vectorizerStatus.candidate.multi_type_proposal.text_anchor_f1.toFixed(4),
            })}
          </p>
          <p className="mt-2 rounded border border-emerald-400/20 bg-emerald-950/20 px-2 py-1 text-xs text-emerald-100">
            {t("description_drafting_status", {
              passed: vectorizerStatus.description_drafting.passed_cases,
              cases: vectorizerStatus.description_drafting.evaluated_cases,
              exact: vectorizerStatus.description_drafting.exact_case_rate.toFixed(2),
              reopen: vectorizerStatus.description_drafting.dxf_reopen_rate.toFixed(2),
            })}
          </p>
          <div className="mt-3 grid gap-2 rounded border border-white/10 bg-black/20 p-3 text-xs text-zinc-300 md:grid-cols-2">
            <div>
              <div className="font-medium text-zinc-200">{t("pipeline_models")}</div>
              <div className="mt-1">
                {t("pipeline_geometry")}: {vectorizerStatus.runtime_pipeline.components.geometry.assignment}
              </div>
              <div>
                {t("pipeline_reader")}: {vectorizerStatus.runtime_pipeline.components.spec_reader.models.map((m) => m.key).join(" → ") || t("pipeline_unassigned")}
              </div>
              <div>
                {t("pipeline_drafter")}: {vectorizerStatus.runtime_pipeline.components.spec_drafter.models.map((m) => m.key).join(" → ") || t("pipeline_deterministic")}
              </div>
            </div>
            <div>
              <div>{t("pipeline_revision")}: {vectorizerStatus.runtime_pipeline.pipeline_revision}</div>
              <div className="font-mono text-[10px] text-zinc-500">
                config {vectorizerStatus.runtime_pipeline.config_sha256.slice(0, 16)}…
              </div>
              <div className="mt-1">
                {t("pipeline_profiles")}: {vectorizerStatus.runtime_pipeline.user_extensible_via.profiles.join(", ")}
              </div>
              <Link
                href={vectorizerStatus.runtime_pipeline.user_extensible_via.model_assignments}
                className="mt-2 inline-block text-sky-300 hover:text-sky-200"
              >
                {t("pipeline_assign_models")}
              </Link>
            </div>
          </div>
        </section>
      )}

      {showNewSheet && (
        <section className="flex flex-wrap items-end gap-3 rounded-lg border border-white/10 bg-zinc-900/40 p-4">
          <label className="flex flex-col gap-1 text-xs text-zinc-400">
            {t("sheet_title")}
            <input
              value={sheetTitle}
              onChange={(e) => setSheetTitle(e.target.value)}
              className="rounded border border-white/15 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
              placeholder={t("sheet_title_placeholder")}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-zinc-400">
            {t("sheet_format")}
            <select
              value={sheetFormat}
              onChange={(e) => setSheetFormat(e.target.value as SheetFormat)}
              className="rounded border border-white/15 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100"
            >
              {(["A4", "A3", "A2", "A1"] as const).map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-zinc-300">
            <input
              type="checkbox"
              checked={landscape}
              onChange={(e) => setLandscape(e.target.checked)}
            />
            {t("landscape")}
          </label>
          <label className="flex items-center gap-2 text-sm text-zinc-300">
            <input
              type="checkbox"
              checked={withFrame}
              onChange={(e) => setWithFrame(e.target.checked)}
            />
            {t("with_frame")}
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => void onCreateSheet()}
            className="rounded bg-sky-600 px-3 py-1.5 text-sm text-white hover:bg-sky-500 disabled:opacity-50"
          >
            {t("create")}
          </button>
        </section>
      )}

      {error && (
        <p className="rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      <input
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={t("search")}
        className="w-full max-w-md rounded border border-white/15 bg-zinc-950 px-3 py-2 text-sm text-zinc-100"
      />

      {filtered.length === 0 ? (
        <p className="py-12 text-center text-sm text-zinc-500">{t("empty")}</p>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
          {filtered.map((g) => {
            const kind = docKind(g);
            const processing = g.status === "queued" || g.status === "running";
            return (
              <Link
                key={g.id}
                href={`/cad/${g.id}`}
                className="group overflow-hidden rounded-lg border border-white/10 bg-zinc-900/40 transition hover:border-sky-500/50"
              >
                <div className="flex aspect-[4/3] items-center justify-center bg-zinc-950">
                  {g.has_result ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={resultUrl(g.id, true)}
                      alt={docTitle(g)}
                      className="h-full w-full object-contain"
                      loading="lazy"
                    />
                  ) : (
                    <span className="text-xs text-zinc-600">
                      {processing ? t("status_processing") : "DXF"}
                    </span>
                  )}
                </div>
                <div className="space-y-1 p-3">
                  <div className="flex items-center gap-1">
                    <p
                      className="flex-1 truncate text-sm text-zinc-200"
                      title={docTitle(g)}
                    >
                      {docTitle(g)}
                    </p>
                    <button
                      type="button"
                      onClick={(ev) => void onRename(g, ev)}
                      title={t("rename")}
                      className="shrink-0 rounded px-1 text-zinc-500 opacity-0 transition hover:bg-white/10 hover:text-zinc-200 group-hover:opacity-100"
                    >
                      ✎
                    </button>
                  </div>
                  <div className="flex flex-wrap items-center gap-1 text-[11px]">
                    <span className="rounded bg-white/5 px-1.5 py-0.5 text-zinc-400">
                      {t(`badge_${kind}`)}
                    </span>
                    {g.status === "failed" ? (
                      <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-red-300">
                        {t("status_failed")}
                      </span>
                    ) : processing ? (
                      <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-300">
                        {t("status_processing")}
                      </span>
                    ) : g.accepted ? (
                      <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-300">
                        {t("status_accepted")}
                      </span>
                    ) : (
                      <span className="rounded bg-sky-500/15 px-1.5 py-0.5 text-sky-300">
                        {t("status_draft")}
                      </span>
                    )}
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </main>
  );
}
