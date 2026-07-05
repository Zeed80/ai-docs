"use client";

// Studio tab «Обучение LoRA»: prepare a dataset from user sources (+synthetic
// ЕСКД sheets), review it, then launch a training run (confirmation dialog in
// this panel — no separate approval step) and watch live progress. Heavy
// lifting is backend-side (/api/lora/*); this panel is a thin, poll-driven
// view. Its job is to answer three questions for a run measured in hours:
// «is it alive?», «when will it finish?», «is it getting better?».

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  checkLora,
  createDataset,
  createRun,
  deleteDataset,
  deleteRun,
  deployCheckpoint,
  getHfTokenStatus,
  HfTokenStatus,
  listBaseModels,
  listCaptionModels,
  listDatasets,
  listLoras,
  listRuns,
  LoraBaseModel,
  LoraCompat,
  LoraLibraryItem,
  LoraDataset,
  LoraProgress,
  LoraRun,
  makeWorkflow,
  previewUrl,
  stopRun,
  uploadLora,
  uploadSource,
} from "@/lib/lora-api";

const PRESETS = [
  {
    value: "drawing_cleanup",
    label: "Очистка чертежей (фото → чистый чертёж)",
    needsSources: true,
    needsCaptions: true,
  },
  {
    value: "drawing_edit",
    label: "Правки чертежей (инструкция → правка)",
    needsSources: false,
    needsCaptions: false,
  },
];

const CAPTION_MODELS = [
  {
    value: "qwen3.6:35b",
    label: "qwen3.6:35b — точнее (медленнее, даунскейл 800px)",
  },
  { value: "gemma4:31b", label: "gemma4:31b — стабильнее" },
  { value: "gemma4:e4b", label: "gemma4:e4b — быстро, поверхностно" },
];

const STATUS_RU: Record<string, string> = {
  preparing: "готовится",
  ready: "готов",
  failed: "ошибка",
  pending_approval: "ждёт подтверждения",
  queued: "в очереди",
  running: "обучается",
  stopping: "останавливается",
  done: "завершён",
  cancelled: "отменён",
};

// ── Small helpers ────────────────────────────────────────────────────────────

function parseHms(hms?: string | null): number | null {
  if (!hms) return null;
  const parts = hms.split(":").map(Number);
  if (parts.some((n) => Number.isNaN(n))) return null;
  const [h, m, s] =
    parts.length === 3
      ? parts
      : parts.length === 2
        ? [0, ...parts]
        : [0, 0, ...parts];
  return h * 3600 + m * 60 + s;
}

/** Remaining seconds: prefer the trainer's own tqdm eta, else scale the
 * catalog estimate by the fraction of steps left. */
function remainingSeconds(run: LoraRun): number | null {
  const fromTqdm = parseHms(run.progress.eta);
  if (fromTqdm != null) return fromTqdm;
  const step = run.progress.step ?? 0;
  const total = run.progress.total ?? Number(run.config?.steps ?? 0);
  if (run.eta_hours && total > 0) {
    return run.eta_hours * 3600 * (1 - step / total);
  }
  return null;
}

function finishClock(secs: number): string {
  const at = new Date(Date.now() + secs * 1000);
  const sameDay = at.toDateString() === new Date().toDateString();
  const time = at.toLocaleTimeString("ru", {
    hour: "2-digit",
    minute: "2-digit",
  });
  if (sameDay) return `сегодня ${time}`;
  const day = at.toLocaleDateString("ru", { day: "numeric", month: "short" });
  return `${day} ${time}`;
}

function humanDuration(secs: number): string {
  if (secs < 90) return `${Math.round(secs)} с`;
  const m = Math.round(secs / 60);
  if (m < 90) return `${m} мин`;
  const h = Math.floor(m / 60);
  return `${h} ч ${m % 60} мин`;
}

/** ai-toolkit sample filenames: `..._{step:09d}_{idx}.jpg`. */
function sampleStep(path: string): number | null {
  const m = path.match(/_(\d{9})_/) ?? path.match(/__(\d{9})_/);
  return m ? Number(m[1]) : null;
}
function sampleIndex(path: string): number {
  const m = path.match(/_(\d+)\.[a-z]+$/i);
  return m ? Number(m[1]) : 0;
}

// ── Loss chart (EMA-smoothed, labelled) ──────────────────────────────────────

function LossChart({ history }: { history: [number, number][] }) {
  if (!history || history.length < 3) return null;
  const w = 260;
  const h = 56;
  const losses = history.map(([, l]) => l);
  const min = Math.min(...losses);
  const max = Math.max(...losses);
  const span = max - min || 1;
  const x = (i: number) => (i / (history.length - 1)) * w;
  const y = (l: number) => h - ((l - min) / span) * (h - 8) - 4;

  const raw = history
    .map(([, l], i) => `${x(i).toFixed(1)},${y(l).toFixed(1)}`)
    .join(" ");
  // EMA over the noisy diffusion loss so the trend is legible.
  const alpha = 0.2;
  let ema = losses[0];
  const emaPts = losses
    .map((l, i) => {
      ema = alpha * l + (1 - alpha) * ema;
      return `${x(i).toFixed(1)},${y(ema).toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg
      width={w}
      height={h}
      className="block"
      role="img"
      aria-label="график loss"
    >
      <polyline
        points={raw}
        fill="none"
        stroke="#38bdf8"
        strokeWidth="1"
        opacity="0.35"
      />
      <polyline
        points={emaPts}
        fill="none"
        stroke="#38bdf8"
        strokeWidth="1.8"
      />
      <text x="0" y="8" className="fill-zinc-500" fontSize="8">
        {max.toFixed(3)}
      </text>
      <text x="0" y={h - 1} className="fill-zinc-500" fontSize="8">
        {min.toFixed(3)}
      </text>
    </svg>
  );
}

/** Text trend from the tail of the EMA: falling / plateau / rising. */
function lossTrend(history?: [number, number][]): string | null {
  if (!history || history.length < 6) return null;
  const losses = history.map(([, l]) => l);
  const alpha = 0.2;
  let ema = losses[0];
  const emaSeries = losses.map((l) => (ema = alpha * l + (1 - alpha) * ema));
  const tail = emaSeries.slice(-Math.min(8, emaSeries.length));
  const first = tail[0];
  const last = tail[tail.length - 1];
  const rel = (last - first) / (Math.abs(first) || 1);
  if (rel < -0.03) return "снижается";
  if (rel > 0.03) {
    const step = history[Math.max(0, history.length - tail.length)][0];
    return `растёт с шага ${step} — возможно переобучение`;
  }
  return "на плато";
}

// ── Sample-evolution grid + lightbox ─────────────────────────────────────────

interface Lightbox {
  paths: string[];
  index: number;
}

function SampleEvolution({
  run,
  onOpen,
}: {
  run: LoraRun;
  onOpen: (lb: Lightbox) => void;
}) {
  const { rows, steps } = useMemo(() => {
    const byIndex = new Map<number, { step: number; path: string }[]>();
    for (const p of run.sample_paths) {
      const idx = sampleIndex(p);
      const st = sampleStep(p) ?? 0;
      const arr = byIndex.get(idx) ?? [];
      arr.push({ step: st, path: p });
      byIndex.set(idx, arr);
    }
    const allSteps = Array.from(
      new Set(run.sample_paths.map((p) => sampleStep(p) ?? 0)),
    ).sort((a, b) => a - b);
    const rows = Array.from(byIndex.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([idx, items]) => ({
        idx,
        control: run.control_paths[idx] ?? null,
        items: items.sort((a, b) => a.step - b.step),
      }));
    return { rows, steps: allSteps };
  }, [run.sample_paths, run.control_paths]);

  if (rows.length === 0) return null;

  return (
    <div className="space-y-2">
      <div className="text-[11px] text-zinc-500">
        Прогресс на контрольных образцах (не входят в обучение)
      </div>
      {rows.map((row) => {
        const rowPaths = [
          ...(row.control ? [row.control] : []),
          ...row.items.map((i) => i.path),
        ];
        return (
          <div key={row.idx} className="flex gap-1.5 overflow-x-auto pb-1">
            {row.control && (
              <figure className="shrink-0">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={previewUrl(row.control)}
                  alt="исходник"
                  onClick={() => onOpen({ paths: rowPaths, index: 0 })}
                  className="h-40 rounded border border-amber-500/40 cursor-zoom-in"
                />
                <figcaption className="text-[10px] text-amber-400/80 text-center mt-0.5">
                  исходник
                </figcaption>
              </figure>
            )}
            {row.items.map((it, i) => (
              <figure key={it.path} className="shrink-0">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={previewUrl(it.path)}
                  alt={`шаг ${it.step}`}
                  onClick={() =>
                    onOpen({ paths: rowPaths, index: row.control ? i + 1 : i })
                  }
                  className="h-40 rounded border border-white/10 cursor-zoom-in"
                />
                <figcaption className="text-[10px] text-zinc-500 text-center mt-0.5">
                  шаг {it.step}
                </figcaption>
              </figure>
            ))}
          </div>
        );
      })}
      {steps.length > 1 && (
        <div className="text-[10px] text-zinc-600">
          шаги: {steps.join(" · ")}
        </div>
      )}
    </div>
  );
}

function LightboxView({
  lb,
  onClose,
  onNav,
}: {
  lb: Lightbox;
  onClose: () => void;
  onNav: (delta: number) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowLeft") onNav(-1);
      if (e.key === "ArrowRight") onNav(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onNav]);

  return (
    <div
      className="fixed inset-0 z-50 bg-black/85 flex items-center justify-center"
      onClick={onClose}
    >
      <button
        aria-label="Назад"
        onClick={(e) => {
          e.stopPropagation();
          onNav(-1);
        }}
        className="absolute left-4 text-white/70 hover:text-white text-4xl px-3"
      >
        ‹
      </button>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={previewUrl(lb.paths[lb.index])}
        alt="образец"
        onClick={(e) => e.stopPropagation()}
        className="max-h-[90vh] max-w-[90vw] rounded shadow-2xl"
      />
      <button
        aria-label="Вперёд"
        onClick={(e) => {
          e.stopPropagation();
          onNav(1);
        }}
        className="absolute right-4 text-white/70 hover:text-white text-4xl px-3"
      >
        ›
      </button>
    </div>
  );
}

// ── Status badge ─────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const color =
    status === "ready" || status === "done"
      ? "bg-emerald-500/15 text-emerald-300"
      : status === "failed"
        ? "bg-red-500/15 text-red-300"
        : status === "running" || status === "preparing"
          ? "bg-sky-500/15 text-sky-300"
          : "bg-zinc-500/15 text-zinc-300";
  return (
    <span className={`px-2 py-0.5 rounded text-[11px] ${color}`}>
      {STATUS_RU[status] ?? status}
    </span>
  );
}

// ── Freshness ("updated N ago") ──────────────────────────────────────────────

function Freshness({ ts, now }: { ts?: number; now: number }) {
  if (!ts) return null;
  const age = Math.max(0, now - ts);
  const color =
    age > 300 ? "text-red-400" : age > 120 ? "text-amber-400" : "text-zinc-500";
  const label =
    age > 300
      ? `связь с тренером потеряна (${humanDuration(age)})`
      : `обновлено ${humanDuration(age)} назад`;
  return <span className={`text-[11px] ${color}`}>{label}</span>;
}

// ── Collapsible form section ─────────────────────────────────────────────────

function Collapsible({
  title,
  defaultOpen,
  children,
}: {
  title: string;
  defaultOpen: boolean;
  children: React.ReactNode;
}) {
  return (
    <details
      open={defaultOpen}
      className="rounded-lg border border-white/10 bg-zinc-900/40"
    >
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-white">
        {title}
      </summary>
      <div className="px-4 pb-4 space-y-3">{children}</div>
    </details>
  );
}

// ── Confirm launch dialog ────────────────────────────────────────────────────

function ConfirmDialog({
  title,
  body,
  confirmLabel,
  danger,
  onConfirm,
  onCancel,
}: {
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 flex items-center justify-center p-4"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-md rounded-lg border border-white/10 bg-zinc-900 p-5 space-y-4"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold text-white">{title}</h3>
        <div className="text-sm text-zinc-300 space-y-2">{body}</div>
        <div className="flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 rounded text-sm text-zinc-300 hover:bg-white/5"
          >
            Отмена
          </button>
          <button
            onClick={onConfirm}
            className={`px-3 py-1.5 rounded text-sm text-white ${
              danger
                ? "bg-red-600 hover:bg-red-500"
                : "bg-emerald-600 hover:bg-emerald-500"
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main panel ───────────────────────────────────────────────────────────────

export default function LoraTrainingPanel() {
  const t = useTranslations("studio");
  const [datasets, setDatasets] = useState<LoraDataset[]>([]);
  const [runs, setRuns] = useState<LoraRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now() / 1000);
  const [lightbox, setLightbox] = useState<Lightbox | null>(null);
  const [busyBtn, setBusyBtn] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Dataset form
  const [dsName, setDsName] = useState("");
  const [preset, setPreset] = useState(PRESETS[0].value);
  const [files, setFiles] = useState<File[]>([]);
  const [synthCount, setSynthCount] = useState(200);
  const [perImage, setPerImage] = useState(2);
  const [captionModel, setCaptionModel] = useState(CAPTION_MODELS[0].value);
  const [creating, setCreating] = useState(false);
  const presetInfo = PRESETS.find((p) => p.value === preset) ?? PRESETS[0];

  // Run form
  const [runDatasetId, setRunDatasetId] = useState("");
  const [runName, setRunName] = useState("");
  const [steps, setSteps] = useState(2500);
  const [rank, setRank] = useState(32);
  const [lr, setLr] = useState("1e-4");
  const [resolution, setResolution] = useState(768);
  const [baseModel, setBaseModel] = useState("qwen_image_edit_2511");
  const [launching, setLaunching] = useState(false);
  const [confirmLaunch, setConfirmLaunch] = useState(false);

  // Fine-tune: continue training an existing LoRA
  const [loras, setLoras] = useState<LoraLibraryItem[]>([]);
  const [resumeLora, setResumeLora] = useState("");
  const [compat, setCompat] = useState<LoraCompat | null>(null);
  const [uploadingLora, setUploadingLora] = useState(false);

  const [captionOptions, setCaptionOptions] = useState(CAPTION_MODELS);
  const [baseModels, setBaseModels] = useState<LoraBaseModel[]>([]);
  const [hfStatus, setHfStatus] = useState<HfTokenStatus | null>(null);

  const loadHfStatus = useCallback(() => {
    getHfTokenStatus()
      .then(setHfStatus)
      .catch(() => undefined);
  }, []);

  const loadLoras = useCallback(() => {
    listLoras()
      .then(setLoras)
      .catch(() => undefined);
  }, []);

  const flashNotice = useCallback((msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), 8000);
  }, []);

  useEffect(() => {
    listCaptionModels()
      .then((models) => {
        if (models.length) {
          setCaptionOptions(
            models.map((m) => ({
              value: m.model,
              label: `${m.model} (${m.provider})`,
            })),
          );
        }
      })
      .catch(() => undefined);
    listBaseModels()
      .then((r) => {
        setBaseModels(r.models);
        setBaseModel(r.default);
      })
      .catch(() => undefined);
    loadHfStatus();
    loadLoras();
  }, [loadHfStatus, loadLoras]);

  // Live compatibility check when a LoRA / base model / rank changes.
  useEffect(() => {
    if (!resumeLora) {
      setCompat(null);
      return;
    }
    let alive = true;
    checkLora(resumeLora, baseModel, rank)
      .then((r) => alive && setCompat(r.check))
      .catch(() => alive && setCompat(null));
    return () => {
      alive = false;
    };
  }, [resumeLora, baseModel, rank]);

  const load = useCallback(async () => {
    try {
      const [ds, rs] = await Promise.all([listDatasets(), listRuns()]);
      setDatasets(ds);
      setRuns(rs);
      setError(null);
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const hasActive =
    datasets.some((d) => d.status === "preparing") ||
    runs.some((r) => ["queued", "running", "stopping"].includes(r.status));

  useEffect(() => {
    if (hasActive && !pollRef.current) {
      pollRef.current = setInterval(load, 5000);
    } else if (!hasActive && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [hasActive, load]);

  // Tick a clock while a run is active so "updated N ago" and the finish
  // estimate stay live between polls.
  useEffect(() => {
    if (!hasActive) return;
    const id = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(id);
  }, [hasActive]);

  const datasetName = useCallback(
    (id: string) => datasets.find((d) => d.id === id)?.name ?? "—",
    [datasets],
  );

  const submitDataset = async () => {
    setCreating(true);
    setError(null);
    try {
      const paths: string[] = [];
      for (const f of files) paths.push((await uploadSource(f)).path);
      await createDataset({
        name: dsName || `Датасет ${new Date().toLocaleDateString("ru")}`,
        preset,
        source_paths: paths,
        params: {
          synth_count: synthCount,
          per_image: perImage,
          caption_model: captionModel,
        },
      });
      setDsName("");
      setFiles([]);
      flashNotice(
        "Датасет создан — подготовка запущена (рендер, деградация, капшены).",
      );
      await load();
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setCreating(false);
    }
  };

  const doLaunch = async () => {
    setConfirmLaunch(false);
    setLaunching(true);
    setError(null);
    try {
      await createRun({
        dataset_id: runDatasetId,
        name: runName || `LoRA ${new Date().toLocaleDateString("ru")}`,
        config: {
          steps,
          rank,
          lr: Number(lr) || 1e-4,
          resolution,
          base_model: baseModel,
        },
        resume_lora: resumeLora || null,
      });
      setRunName("");
      setResumeLora("");
      flashNotice("Обучение поставлено в очередь. Прогресс — в списке справа.");
      await load();
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setLaunching(false);
    }
  };

  const onUploadLora = async (file: File) => {
    setUploadingLora(true);
    setError(null);
    try {
      const item = await uploadLora(file);
      loadLoras();
      setResumeLora(item.ref);
      flashNotice(
        `LoRA «${item.label}» загружена (семейство ${item.family ?? "?"}, rank ${item.rank ?? "?"}).`,
      );
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setUploadingLora(false);
    }
  };

  const onStop = async (r: LoraRun) => {
    try {
      await stopRun(r.id);
      await load();
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  };

  const withBusy = async (key: string, fn: () => Promise<void>) => {
    setBusyBtn(key);
    try {
      await fn();
    } finally {
      setBusyBtn(null);
    }
  };

  const onDeploy = (r: LoraRun, ckpt: string) =>
    withBusy(`deploy:${r.id}:${ckpt}`, async () => {
      try {
        const res = await deployCheckpoint(r.id, ckpt);
        flashNotice(
          `LoRA скопирована на узел ComfyUI как «${res.lora_name}» — можно указывать в воркфлоу.`,
        );
      } catch (e) {
        setError(String((e as Error).message || e));
      }
    });

  const onMakeWorkflow = (r: LoraRun, ckpt: string) =>
    withBusy(`wf:${r.id}:${ckpt}`, async () => {
      try {
        const res = await makeWorkflow(r.id, ckpt);
        flashNotice(
          `Воркфлоу «${res.title}» создан — выберите его в форме генерации студии.`,
        );
      } catch (e) {
        setError(String((e as Error).message || e));
      }
    });

  const navLightbox = (delta: number) =>
    setLightbox((lb) =>
      lb
        ? {
            ...lb,
            index: (lb.index + delta + lb.paths.length) % lb.paths.length,
          }
        : lb,
    );

  const readyDatasets = datasets.filter((d) => d.status === "ready");
  const activeRuns = runs.filter((r) =>
    ["queued", "running", "stopping"].includes(r.status),
  );
  const baseInfo = baseModels.find((m) => m.key === baseModel);
  const etaText =
    baseInfo?.sec_per_step != null
      ? `~${((steps * baseInfo.sec_per_step) / 3600).toFixed(1)} ч`
      : "оценка появится после первых шагов";

  return (
    <div className="p-4 space-y-4">
      {lightbox && (
        <LightboxView
          lb={lightbox}
          onClose={() => setLightbox(null)}
          onNav={navLightbox}
        />
      )}
      {confirmLaunch && (
        <ConfirmDialog
          title="Запустить обучение LoRA?"
          confirmLabel="Запустить"
          onConfirm={doLaunch}
          onCancel={() => setConfirmLaunch(false)}
          body={
            <>
              <p>
                Датасет: <b>{datasetName(runDatasetId)}</b>. Базовая модель:{" "}
                <b>{baseInfo?.label ?? baseModel}</b>. Шагов: <b>{steps}</b>.
              </p>
              <p>
                Ориентировочно займёт <b>{etaText}</b>. На это время локальный
                GPU будет занят: локальные ИИ-функции (агент, OCR, студия)
                станут недоступны, облачные маршруты продолжат работать.
              </p>
              {baseInfo && !baseInfo.fits_24gb && (
                <p className="text-amber-400">
                  ⚠{" "}
                  {baseInfo.vram_note ??
                    "модель может не поместиться в память GPU"}
                  .
                </p>
              )}
            </>
          }
        />
      )}

      {error && (
        <div className="text-xs text-red-400 bg-red-500/10 rounded p-2">
          {error}
        </div>
      )}
      {notice && (
        <div className="text-xs text-emerald-300 bg-emerald-500/10 rounded p-2">
          {notice}
        </div>
      )}

      {/* ── Active runs first — that's what the user opens the tab for ── */}
      {activeRuns.length > 0 && (
        <div className="space-y-3">
          {activeRuns.map((r) => (
            <RunCard
              key={r.id}
              run={r}
              now={now}
              datasetName={datasetName(r.dataset_id)}
              busyBtn={busyBtn}
              onStop={onStop}
              onDeploy={onDeploy}
              onMakeWorkflow={onMakeWorkflow}
              onDelete={async () => {
                try {
                  await deleteRun(r.id);
                  await load();
                } catch (e) {
                  setError(String((e as Error).message || e));
                }
              }}
              onOpenSample={setLightbox}
            />
          ))}
        </div>
      )}

      <div className="grid gap-4 xl:grid-cols-2">
        {/* ── Datasets column ── */}
        <div className="space-y-4">
          <Collapsible title={t("lora_new_dataset")} defaultOpen={!hasActive}>
            <input
              value={dsName}
              onChange={(e) => setDsName(e.target.value)}
              placeholder={t("lora_dataset_name")}
              className="w-full bg-zinc-800 rounded px-3 py-1.5 text-sm text-white"
            />
            <label className="text-xs text-zinc-400 block">
              {t("lora_preset")}
              <select
                value={preset}
                onChange={(e) => setPreset(e.target.value)}
                className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white mt-1"
              >
                {PRESETS.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>
            {presetInfo.needsSources && (
              <div>
                <label className="text-xs text-zinc-400 block mb-1">
                  {t("lora_sources_hint")}
                </label>
                <input
                  type="file"
                  multiple
                  accept=".png,.jpg,.jpeg,.dxf,.dwg,.pdf"
                  onChange={(e) => setFiles(Array.from(e.target.files ?? []))}
                  className="text-xs text-zinc-400"
                />
              </div>
            )}
            <div className="grid grid-cols-2 gap-2">
              <label className="text-xs text-zinc-400">
                {t("lora_synth_count")}
                <input
                  type="number"
                  min={0}
                  max={2000}
                  value={synthCount}
                  onChange={(e) => setSynthCount(Number(e.target.value))}
                  className="w-full bg-zinc-800 rounded px-2 py-1 text-sm text-white mt-1"
                />
              </label>
              <label className="text-xs text-zinc-400">
                {t("lora_per_image")}
                <input
                  type="number"
                  min={1}
                  max={6}
                  value={perImage}
                  onChange={(e) => setPerImage(Number(e.target.value))}
                  className="w-full bg-zinc-800 rounded px-2 py-1 text-sm text-white mt-1"
                />
              </label>
            </div>
            {presetInfo.needsCaptions && (
              <label className="text-xs text-zinc-400 block">
                {t("lora_caption_model")}
                <select
                  value={captionModel}
                  onChange={(e) => setCaptionModel(e.target.value)}
                  className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white mt-1"
                >
                  {captionOptions.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <button
              onClick={submitDataset}
              disabled={creating || (files.length === 0 && synthCount <= 0)}
              className="w-full py-2 rounded bg-sky-600 hover:bg-sky-500 disabled:opacity-40 text-sm text-white"
            >
              {creating ? t("lora_creating") : t("lora_create_dataset")}
            </button>
          </Collapsible>

          {datasets.length === 0 && (
            <p className="text-xs text-zinc-500 px-1">
              Датасетов пока нет — создайте первый выше.
            </p>
          )}
          {datasets.map((ds) => (
            <DatasetCard
              key={ds.id}
              ds={ds}
              t={t}
              onDelete={async () => {
                try {
                  await deleteDataset(ds.id);
                  await load();
                } catch (e) {
                  setError(String((e as Error).message || e));
                }
              }}
            />
          ))}
        </div>

        {/* ── Runs form + finished runs ── */}
        <div className="space-y-4">
          <Collapsible title={t("lora_new_run")} defaultOpen={!hasActive}>
            <select
              value={runDatasetId}
              onChange={(e) => setRunDatasetId(e.target.value)}
              className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white"
            >
              <option value="">{t("lora_pick_dataset")}</option>
              {readyDatasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
            <input
              value={runName}
              onChange={(e) => setRunName(e.target.value)}
              placeholder={t("lora_run_name")}
              className="w-full bg-zinc-800 rounded px-3 py-1.5 text-sm text-white"
            />
            <label className="text-xs text-zinc-400 block">
              {t("lora_base_model")}
              <select
                value={baseModel}
                onChange={(e) => setBaseModel(e.target.value)}
                className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white mt-1"
              >
                {baseModels.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.label}
                    {!m.fits_24gb ? " ⚠" : ""}
                  </option>
                ))}
              </select>
            </label>
            {baseInfo && !baseInfo.fits_24gb && (
              <p className="text-[11px] text-amber-400">
                ⚠ {baseInfo.vram_note}
              </p>
            )}
            {baseInfo?.gated &&
              (hfStatus?.configured ? (
                <p className="text-[11px] text-emerald-400">
                  🔒 Закрытая модель — токен HuggingFace задан. Не забудьте
                  принять лицензию на странице модели.
                </p>
              ) : (
                <p className="text-[11px] text-amber-400">
                  🔒 Закрытая модель HuggingFace: примите лицензию на её
                  странице и задайте токен в{" "}
                  <a href="/settings/models" className="underline">
                    Настройки → Модели
                  </a>{" "}
                  (🤗 HuggingFace). FLUX.2 klein 4B — без токена.
                </p>
              ))}
            <label className="text-xs text-zinc-400 block">
              {t("lora_steps")} ({etaText})
              <input
                type="number"
                min={100}
                max={20000}
                step={250}
                value={steps}
                onChange={(e) => setSteps(Number(e.target.value))}
                className="w-full bg-zinc-800 rounded px-2 py-1 text-sm text-white mt-1"
              />
            </label>
            <div className="grid grid-cols-3 gap-2">
              <label className="text-xs text-zinc-400">
                rank
                <input
                  type="number"
                  min={4}
                  max={128}
                  value={rank}
                  onChange={(e) => setRank(Number(e.target.value))}
                  className="w-full bg-zinc-800 rounded px-2 py-1 text-sm text-white mt-1"
                />
              </label>
              <label className="text-xs text-zinc-400">
                lr
                <input
                  value={lr}
                  onChange={(e) => setLr(e.target.value)}
                  className="w-full bg-zinc-800 rounded px-2 py-1 text-sm text-white mt-1"
                />
              </label>
              <label className="text-xs text-zinc-400">
                {t("lora_resolution")}
                <select
                  value={resolution}
                  onChange={(e) => setResolution(Number(e.target.value))}
                  className="w-full bg-zinc-800 rounded px-2 py-1 text-sm text-white mt-1"
                >
                  <option value={512}>512</option>
                  <option value={768}>768</option>
                  <option value={1024}>1024</option>
                </select>
              </label>
            </div>

            {/* Fine-tune: continue an existing LoRA */}
            <details className="rounded border border-white/10 bg-zinc-900/40 p-2">
              <summary className="cursor-pointer text-xs text-zinc-300">
                Дообучить существующую LoRA (необязательно)
              </summary>
              <div className="mt-2 space-y-2">
                <select
                  value={resumeLora}
                  onChange={(e) => setResumeLora(e.target.value)}
                  className="w-full bg-zinc-800 rounded px-2 py-1.5 text-sm text-white"
                >
                  <option value="">— обучать с нуля —</option>
                  {["run", "upload", "node"].map((src) => {
                    const group = loras.filter((l) => l.source === src);
                    if (!group.length) return null;
                    const title = {
                      run: "Свои чекпойнты",
                      upload: "Загруженные",
                      node: "На узле ComfyUI",
                    }[src];
                    return (
                      <optgroup key={src} label={title}>
                        {group.map((l) => (
                          <option key={l.ref} value={l.ref}>
                            {l.label}
                            {l.family ? ` · ${l.family}` : ""}
                            {l.rank ? ` · rank ${l.rank}` : ""}
                          </option>
                        ))}
                      </optgroup>
                    );
                  })}
                </select>
                <label className="text-[11px] text-zinc-400 block">
                  или загрузите .safetensors (своя/сторонняя):
                  <input
                    type="file"
                    accept=".safetensors"
                    disabled={uploadingLora}
                    onChange={(e) =>
                      e.target.files?.[0] && onUploadLora(e.target.files[0])
                    }
                    className="block text-xs text-zinc-400 mt-1"
                  />
                </label>
                {compat && (
                  <div
                    className={`text-[11px] rounded p-2 ${
                      compat.level === "error"
                        ? "bg-red-500/10 text-red-300"
                        : compat.level === "warn"
                          ? "bg-amber-500/10 text-amber-300"
                          : "bg-emerald-500/10 text-emerald-300"
                    }`}
                  >
                    {compat.level === "error"
                      ? "❌ Несовместима"
                      : compat.level === "warn"
                        ? "⚠ Совместимость не подтверждена"
                        : "✓ Совместима"}
                    <ul className="mt-1 list-disc list-inside space-y-0.5">
                      {compat.reasons.map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            </details>

            <button
              onClick={() => setConfirmLaunch(true)}
              disabled={
                launching ||
                !runDatasetId ||
                (!!resumeLora && compat?.level === "error")
              }
              className="w-full py-2 rounded bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-sm text-white"
            >
              {launching ? t("lora_creating") : t("lora_start_training")}
            </button>
          </Collapsible>

          {runs.length === 0 && (
            <p className="text-xs text-zinc-500 px-1">
              Запусков пока нет — подготовьте датасет и запустите обучение.
            </p>
          )}
          {runs
            .filter(
              (r) => !["queued", "running", "stopping"].includes(r.status),
            )
            .map((r) => (
              <RunCard
                key={r.id}
                run={r}
                now={now}
                datasetName={datasetName(r.dataset_id)}
                busyBtn={busyBtn}
                onStop={onStop}
                onDeploy={onDeploy}
                onMakeWorkflow={onMakeWorkflow}
                onDelete={async () => {
                  try {
                    await deleteRun(r.id);
                    await load();
                  } catch (e) {
                    setError(String((e as Error).message || e));
                  }
                }}
                onOpenSample={setLightbox}
              />
            ))}
        </div>
      </div>
    </div>
  );
}

// ── Dataset card ─────────────────────────────────────────────────────────────

function DatasetCard({
  ds,
  t,
  onDelete,
}: {
  ds: LoraDataset;
  t: ReturnType<typeof useTranslations>;
  onDelete: () => void;
}) {
  const s = ds.stats ?? {};
  const rejected = s.pair_rejected ?? [];
  return (
    <div className="rounded-lg border border-white/10 bg-zinc-900/40 p-4 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm text-white font-medium">{ds.name}</span>
        <span className="flex items-center gap-2">
          <StatusBadge status={ds.status} />
          {ds.status !== "preparing" && (
            <button
              onClick={onDelete}
              title={t("lora_delete")}
              aria-label={t("lora_delete")}
              className="text-zinc-500 hover:text-red-400 text-xs"
            >
              ✕
            </button>
          )}
        </span>
      </div>
      {ds.status === "ready" && (
        <div className="text-xs text-zinc-400 flex flex-wrap gap-x-3 gap-y-0.5">
          <span>
            {t("lora_pairs")}: {s.pairs ?? 0}
          </span>
          {(s.holdout ?? 0) > 0 && <span>holdout: {s.holdout}</span>}
          <span>
            {t("lora_captioned")}: {s.captioned ?? 0}
          </span>
          {(s.caption_rejected ?? 0) > 0 && (
            <span className="text-amber-400">
              отклонено описаний: {s.caption_rejected}
            </span>
          )}
          {(s.page_skipped ?? 0) > 0 && (
            <span className="text-amber-400">
              пропущено страниц: {s.page_skipped}
            </span>
          )}
          {(s.render_failed?.length ?? 0) > 0 && (
            <span className="text-amber-400">
              {t("lora_render_failed")}: {s.render_failed!.length}
            </span>
          )}
        </div>
      )}
      {rejected.length > 0 && (
        <details className="text-[11px] text-zinc-500">
          <summary className="cursor-pointer">
            отклонённые пары: {rejected.length}
          </summary>
          <ul className="mt-1 space-y-0.5 max-h-32 overflow-y-auto">
            {rejected.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </details>
      )}
      {ds.error && <div className="text-xs text-red-400">{ds.error}</div>}
      {ds.preview_paths.length > 0 && (
        <div className="flex gap-2 overflow-x-auto">
          {ds.preview_paths.map((p) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              key={p}
              src={previewUrl(p)}
              alt="пара control/target"
              className="h-24 rounded border border-white/10"
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Run card ─────────────────────────────────────────────────────────────────

function RunCard({
  run,
  now,
  datasetName,
  busyBtn,
  onStop,
  onDeploy,
  onMakeWorkflow,
  onDelete,
  onOpenSample,
}: {
  run: LoraRun;
  now: number;
  datasetName: string;
  busyBtn: string | null;
  onStop: (r: LoraRun) => void;
  onDeploy: (r: LoraRun, ckpt: string) => void;
  onMakeWorkflow: (r: LoraRun, ckpt: string) => void;
  onDelete: () => void;
  onOpenSample: (lb: Lightbox) => void;
}) {
  const t = useTranslations("studio");
  const [confirmStop, setConfirmStop] = useState(false);
  const p: LoraProgress = run.progress ?? {};
  const step = p.step ?? 0;
  const total = p.total ?? Number(run.config?.steps ?? 0) ?? 0;
  const pct = total ? Math.round((step / total) * 100) : 0;
  const rem = run.status === "running" ? remainingSeconds(run) : null;
  const trend = lossTrend(p.history);
  const indeterminate =
    run.status === "queued" || (run.status === "running" && !p.step);

  const cfg = run.config ?? {};
  const elapsed =
    run.started_at && run.finished_at
      ? (new Date(run.finished_at).getTime() -
          new Date(run.started_at).getTime()) /
        1000
      : null;

  return (
    <div className="rounded-lg border border-white/10 bg-zinc-900/40 p-4 space-y-2">
      {confirmStop && (
        <ConfirmDialog
          title="Остановить обучение?"
          confirmLabel="Остановить"
          danger
          onConfirm={() => {
            setConfirmStop(false);
            onStop(run);
          }}
          onCancel={() => setConfirmStop(false)}
          body={
            <p>
              {run.started_at
                ? `Идёт ${humanDuration((Date.now() - new Date(run.started_at).getTime()) / 1000)}. `
                : ""}
              Уже сохранённые чекпойнты останутся, но продолжить с этого места
              из интерфейса нельзя.
            </p>
          }
        />
      )}

      {/* Header */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="text-sm text-white font-medium truncate">
            {run.name}
          </div>
          <div className="text-[11px] text-zinc-500 truncate">
            {datasetName} · {run.base_family} · {String(cfg.steps ?? total)}{" "}
            шагов · rank {String(cfg.rank ?? "?")} ·{" "}
            {String(cfg.resolution ?? "?")}px
          </div>
        </div>
        <span className="flex items-center gap-2 shrink-0">
          <StatusBadge status={run.status} />
          {["done", "failed", "cancelled", "pending_approval"].includes(
            run.status,
          ) && (
            <button
              onClick={onDelete}
              title={t("lora_delete")}
              aria-label={t("lora_delete")}
              className="text-zinc-500 hover:text-red-400 text-xs"
            >
              ✕
            </button>
          )}
        </span>
      </div>

      {/* Queued / stopping notes */}
      {run.status === "queued" && (
        <div className="text-xs text-zinc-400">
          В очереди: ожидание GPU, затем загрузка и квантизация базовой модели
          (~10–15 мин до первых шагов).
        </div>
      )}
      {run.status === "stopping" && (
        <div className="text-xs text-amber-400">
          Останавливаем — может занять пару минут (ждём завершения текущего
          шага).
        </div>
      )}

      {/* Progress */}
      {(run.status === "running" ||
        run.status === "queued" ||
        run.status === "stopping") && (
        <>
          <div
            className="h-2 rounded bg-zinc-800 overflow-hidden"
            role="progressbar"
            aria-valuenow={indeterminate ? undefined : pct}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div
              className={`h-full bg-sky-500 ${
                indeterminate ? "animate-pulse w-1/3" : "transition-all"
              }`}
              style={indeterminate ? undefined : { width: `${pct}%` }}
            />
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-400">
            <span>{p.phase ?? ""}</span>
            {run.status === "running" && (
              <span>
                {t("lora_step")} {step}/{total} ({pct}%)
              </span>
            )}
            {p.loss != null && <span>loss {Number(p.loss).toFixed(4)}</span>}
            {rem != null && <span>закончится ≈ {finishClock(rem)}</span>}
            <Freshness ts={p.ts} now={now} />
          </div>
          {run.status === "running" && (
            <button
              onClick={() => setConfirmStop(true)}
              className="text-xs text-red-300 hover:text-red-200"
            >
              {t("lora_stop")}
            </button>
          )}
        </>
      )}

      {/* Loss chart + trend */}
      {(p.history?.length ?? 0) >= 3 && (
        <div>
          <div className="flex items-center justify-between text-[10px] text-zinc-500">
            <span>loss</span>
            {trend && <span>{trend}</span>}
          </div>
          <LossChart history={p.history!} />
        </div>
      )}

      {/* Finished summary */}
      {run.status === "done" && (
        <div className="text-xs text-emerald-300/80">
          Готово{elapsed ? ` за ${humanDuration(elapsed)}` : ""}
          {p.loss != null
            ? ` · финальный loss ${Number(p.loss).toFixed(4)}`
            : ""}
          .
        </div>
      )}

      {/* Error */}
      {run.error && (
        <details className="text-xs text-red-400">
          <summary className="cursor-pointer">ошибка — показать</summary>
          <pre className="mt-1 whitespace-pre-wrap text-[11px] max-h-40 overflow-y-auto bg-black/30 rounded p-2">
            {run.error}
          </pre>
          <button
            onClick={() => navigator.clipboard?.writeText(run.error ?? "")}
            className="mt-1 text-[11px] text-zinc-400 hover:text-white"
          >
            копировать
          </button>
        </details>
      )}

      {/* Sample evolution */}
      {run.sample_paths.length > 0 && (
        <SampleEvolution run={run} onOpen={onOpenSample} />
      )}

      {/* Checkpoints */}
      {run.checkpoints.length > 0 && (
        <div className="space-y-1">
          <div className="text-xs text-zinc-500">{t("lora_checkpoints")}:</div>
          {run.checkpoints.map((c) => {
            const st = sampleStep(c);
            const deployKey = `deploy:${run.id}:${c}`;
            const wfKey = `wf:${run.id}:${c}`;
            return (
              <div
                key={c}
                className="flex items-center justify-between text-xs text-zinc-300"
              >
                <span className="truncate" title={c}>
                  {st != null ? `шаг ${st}` : c}
                </span>
                <span className="flex gap-1 shrink-0 ml-2">
                  <button
                    onClick={() => onDeploy(run, c)}
                    disabled={busyBtn === deployKey}
                    className="px-2 py-0.5 rounded bg-white/10 hover:bg-white/20 disabled:opacity-40"
                  >
                    {t("lora_deploy")}
                  </button>
                  <button
                    onClick={() => onMakeWorkflow(run, c)}
                    disabled={busyBtn === wfKey}
                    className="px-2 py-0.5 rounded bg-emerald-600/70 hover:bg-emerald-500/70 text-white disabled:opacity-40"
                  >
                    {t("lora_make_workflow")}
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
