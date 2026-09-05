"use client";

/**
 * Shared pipeline rendering. Extracted from app/documents/page.tsx so the
 * supplier-catalog pipeline (its own stage list, same DocumentProcessingJob
 * storage) shows the same way instead of getting a second, drifting copy.
 *
 * Stage labels fall back to `label ?? key`, so a pipeline with unknown keys —
 * like the catalog one — renders correctly with no change here.
 */

export interface PipelineStep {
  key: string;
  label?: string;
  status:
    | "pending"
    | "queued"
    | "running"
    | "done"
    | "failed"
    | "skipped"
    | string;
  error?: string | null;
  progress?: { done?: number; total?: number } | null;
}

export interface PipelineStatusLike {
  processing_status?: string | null;
  current_step?: string | null;
  processing_error?: string | null;
  pipeline_steps?: PipelineStep[];
}

export const PIPELINE_STEP_LABELS: Record<string, string> = {
  store: "Файл",
  memory_seed: "Память",
  classification: "Класс",
  extraction: "OCR",
  sql_records: "SQL",
  memory_graph: "Граф",
  embedding: "Векторы",
  // catalog pipeline
  unpack: "Архив",
  parse: "Разбор",
  normalize: "Нормализация",
  entries: "Позиции",
  canonical: "Номенклатура",
  graph: "Граф",
};

export const CURRENT_STEP_LABELS: Record<string, string> = {
  store: "Сохранение файла",
  memory_seed: "Первичная память",
  classification: "Классификация",
  extraction: "Извлечение данных",
  sql_records: "Сохранение записей",
  memory_graph: "Построение графа",
  embedding: "Векторизация",
  unpack: "Распаковка архива",
  parse: "Разбор каталога",
  normalize: "Нормализация строк",
  entries: "Создание позиций",
  canonical: "Сопоставление номенклатуры",
  graph: "Память и граф",
  completed: "Завершён",
  watchdog_reset: "Сброс по таймауту",
};

export const FALLBACK_PROCESS_STEPS: PipelineStep[] = [
  { key: "store", label: "Файл", status: "pending" },
  { key: "memory_seed", label: "Память", status: "pending" },
  { key: "classification", label: "Класс", status: "pending" },
  { key: "extraction", label: "OCR", status: "pending" },
  { key: "sql_records", label: "SQL", status: "pending" },
  { key: "memory_graph", label: "Граф", status: "pending" },
  { key: "embedding", label: "Векторы", status: "pending" },
];

export function pipelineSteps(
  pipeline: PipelineStatusLike | null | undefined,
  fallback: PipelineStep[] = FALLBACK_PROCESS_STEPS,
): PipelineStep[] {
  const steps = pipeline?.pipeline_steps?.length
    ? pipeline.pipeline_steps
    : fallback;
  return steps
    .filter((step) => step.key !== "ntd")
    .map((step) => ({
      ...step,
      label: PIPELINE_STEP_LABELS[step.key] ?? step.label ?? step.key,
    }));
}

export function pipelineProgress(
  pipeline: PipelineStatusLike | null | undefined,
  fallback: PipelineStep[] = FALLBACK_PROCESS_STEPS,
) {
  const steps = pipelineSteps(pipeline, fallback);
  if (!steps.length) return 0;
  const completed = steps.filter((step) =>
    ["done", "skipped"].includes(step.status),
  ).length;
  return Math.round((completed / steps.length) * 100);
}

export function ProgressBar({
  value,
  failed = false,
}: {
  value: number;
  failed?: boolean;
}) {
  return (
    <div>
      <div className="mb-1 text-xs text-slate-400">{value}%</div>
      <div className="h-2 overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full ${failed ? "bg-red-500" : "bg-emerald-500"}`}
          style={{ width: `${Math.max(4, value)}%` }}
        />
      </div>
    </div>
  );
}

export function PipelineSteps({
  steps,
  compact = false,
}: {
  steps: PipelineStep[];
  compact?: boolean;
}) {
  const colors: Record<string, string> = {
    done: "border-emerald-900 bg-emerald-950/50 text-emerald-200",
    skipped: "border-slate-700 bg-slate-900 text-slate-400",
    running: "border-blue-800 bg-blue-950/60 text-blue-200",
    queued: "border-amber-800 bg-amber-950/40 text-amber-200",
    failed: "border-red-800 bg-red-950/50 text-red-200",
    pending: "border-slate-800 bg-slate-950 text-slate-400",
  };
  return (
    <div className={`flex flex-wrap ${compact ? "gap-1" : "gap-2"}`}>
      {steps.map((step) => {
        const counter =
          step.progress && step.progress.total
            ? ` ${step.progress.done ?? 0}/${step.progress.total}`
            : "";
        return (
          <span
            key={step.key}
            title={step.error ?? step.status}
            className={`rounded-md border px-2 py-1 text-[11px] ${
              colors[step.status] ?? colors.pending
            }`}
          >
            {step.label}
            {counter}
          </span>
        );
      })}
    </div>
  );
}

export function PipelineProgressCard({
  pipeline,
  fallback,
}: {
  pipeline: PipelineStatusLike | null;
  fallback?: PipelineStep[];
}) {
  const progress = pipelineProgress(pipeline, fallback);
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900 p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <p className="text-xs text-slate-400">Пайплайн</p>
          <p className="mt-1 text-sm text-slate-300">
            {pipeline?.processing_status === "running"
              ? "выполняется"
              : pipeline?.processing_status === "done"
                ? "завершён"
                : pipeline?.processing_status === "failed"
                  ? "ошибка"
                  : pipeline?.processing_status === "queued"
                    ? "в очереди"
                    : "нет задачи"}
            {pipeline?.current_step
              ? ` · ${CURRENT_STEP_LABELS[pipeline.current_step] ?? pipeline.current_step}`
              : ""}
          </p>
        </div>
        <div className="w-24">
          <ProgressBar
            value={progress}
            failed={Boolean(pipeline?.processing_error)}
          />
        </div>
      </div>
      <PipelineSteps steps={pipelineSteps(pipeline, fallback)} />
    </div>
  );
}
