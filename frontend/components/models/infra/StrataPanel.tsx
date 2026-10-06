"use client";

/**
 * Видеокарта: Ollama ⇄ Strata.
 *
 * Strata держит одну большую модель (Qwen3.8-Flash-Next, 125B MoE) на GPU и в
 * оперативной памяти и занимает карту целиком, поэтому рядом с GPU-Ollama не
 * работает: оператор переключает видеокарту между ними. Переключение — явное
 * решение, и оно может переназначить слоты; что именно переедет, показывается
 * до нажатия. Здесь же выбирается квантование: новое скачивается при первом
 * запуске Strata (66–84 ГБ).
 */

import { useCallback, useEffect, useState } from "react";
import { useToast } from "@/components/ui/primitives/Toast";
import {
  btn,
  card,
  cardHeader as cardH,
  select,
} from "@/components/ui/primitives/tokens";
import {
  strataConfig,
  strataAccess,
  strataDeleteQuant,
  strataInstall,
  strataRuntime,
  strataSlotPlan,
  strataStatus,
  strataSwitch,
} from "@/lib/models/api";
import type {
  StrataAccess,
  StrataPhase,
  StrataSlotPlanItem,
  StrataStatus,
} from "@/lib/models/types";

const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200 disabled:opacity-50`;

const PHASE: Record<StrataPhase, { text: string; cls: string }> = {
  not_created: {
    text: "контейнер не создан",
    cls: "bg-slate-700 text-slate-300",
  },
  stopped: { text: "выключена", cls: "bg-slate-700 text-slate-300" },
  installing: {
    text: "скачивание и установка",
    cls: "bg-amber-900/60 text-amber-200",
  },
  loading: { text: "загрузка в память", cls: "bg-amber-900/60 text-amber-200" },
  ready: { text: "работает", cls: "bg-emerald-900/60 text-emerald-200" },
  unloaded: {
    text: "выгружена по простою — загрузится при запросе",
    cls: "bg-slate-700 text-slate-300",
  },
};

const ctxLabel = (n: number) => `${Math.round(n / 1024)}K`;

export function StrataPanel() {
  const toast = useToast();
  const [st, setSt] = useState<StrataStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"strata" | "ollama" | null>(null);
  const [plan, setPlan] = useState<StrataSlotPlanItem[] | null>(null);
  const [moveSlots, setMoveSlots] = useState<Set<string>>(new Set());
  const [restore, setRestore] = useState(true);
  const [draft, setDraft] = useState<{
    model: string;
    context: number;
    vision: boolean;
  } | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await strataStatus();
      setSt(s);
      setLoadError(null);
      setDraft((d) => d ?? { ...s.desired });
    } catch (e) {
      setLoadError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Пока идёт установка/загрузка — опрашиваем часто, иначе редко.
  const active = st?.phase === "installing" || st?.phase === "loading";
  useEffect(() => {
    const id = setInterval(() => void load(), active ? 5000 : 20000);
    return () => clearInterval(id);
  }, [active, load]);

  const openConfirm = async (target: "strata" | "ollama") => {
    setConfirm(target);
    if (target === "strata") {
      try {
        const p = await strataSlotPlan();
        setPlan(p);
        setMoveSlots(new Set(p.filter((i) => i.move).map((i) => i.slot)));
      } catch (e) {
        toast.error(
          "Не удалось получить список слотов",
          String((e as Error).message ?? e),
        );
      }
    }
  };

  const doSwitch = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      const r = await strataSwitch(
        confirm === "strata"
          ? { target: "strata", move_slots: [...moveSlots] }
          : { target: "ollama", restore_slots: restore },
      );
      setSt(r.status);
      setConfirm(null);
      if (confirm === "strata") {
        toast.ok(
          "Видеокарта отдана Strata",
          `Перенесено слотов: ${r.moved.length}; выгружено из Ollama: ${r.ollama_unloaded.length}. Загрузка модели 1–3 мин.`,
        );
      } else {
        toast.ok(
          "Видеокарта возвращена Ollama",
          `Возвращено слотов: ${r.restored.length}`,
        );
      }
    } catch (e) {
      toast.error(
        "Переключение не выполнено",
        String((e as Error).message ?? e),
      );
    } finally {
      setBusy(false);
    }
  };

  const install = async () => {
    setBusy(true);
    try {
      const r = await strataInstall();
      setSt(r.status);
      toast.ok(
        "Скачивание Strata началось",
        "Видеокарта остаётся у Ollama. Прогресс — в журнале ниже; прерванная загрузка продолжится.",
      );
    } catch (e) {
      toast.error("Скачивание не запущено", String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const stopInstall = async () => {
    setBusy(true);
    try {
      const r = await strataSwitch({ target: "ollama", restore_slots: false });
      setSt(r.status);
      toast.ok(
        "Скачивание остановлено",
        "Повторный запуск продолжит с места остановки",
      );
    } catch (e) {
      toast.error("Не удалось остановить", String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const applyConfig = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      const r = await strataConfig(draft);
      setSt(r.status);
      setDraft({ ...r.status.desired });
      toast.ok(
        "Настройки Strata сохранены",
        r.restarted
          ? "Strata перезапускается"
          : "Применятся при следующем включении",
      );
    } catch (e) {
      toast.error(
        "Не удалось сохранить настройки",
        String((e as Error).message ?? e),
      );
    } finally {
      setBusy(false);
    }
  };

  if (!st) {
    return (
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-medium text-slate-100">
            Видеокарта: Ollama ⇄ Strata
          </span>
        </div>
        <div className="p-4 text-sm text-slate-400">
          {loadError ? `Статус Strata недоступен: ${loadError}` : "Загрузка..."}
        </div>
      </div>
    );
  }

  const owner = st.owner ?? "ollama";
  const installOnly = st.desired.install_only && st.container === "running";
  const phase = PHASE[st.phase];
  const desiredQuant = st.quants.find((q) => q.model === draft?.model);
  const draftChanged =
    !!draft &&
    (draft.model !== st.desired.model ||
      draft.context !== st.desired.context ||
      draft.vision !== st.desired.vision);
  const switchingTo = st.quants.find((q) => q.model === st.desired.model);
  const needsDownload = !!switchingTo && !switchingTo.installed;

  return (
    <div className={card}>
      <div className={`${cardH} flex-wrap gap-2`}>
        <span className="text-sm font-medium text-slate-100">
          Видеокарта: Ollama ⇄ Strata
        </span>
        <span className={`text-xs px-2 py-0.5 rounded ${phase.cls}`}>
          Strata: {phase.text}
        </span>
      </div>
      <div className="p-4 space-y-4">
        {/* Переключатель */}
        <div className="flex flex-wrap items-center gap-3">
          <div
            role="radiogroup"
            aria-label="Кому отдана видеокарта"
            className="inline-flex rounded border border-slate-600 overflow-hidden"
          >
            {(["ollama", "strata"] as const).map((t) => (
              <button
                key={t}
                type="button"
                role="radio"
                aria-checked={owner === t}
                disabled={busy || st.phase === "not_created" || installOnly}
                onClick={() => owner !== t && void openConfirm(t)}
                className={`px-4 py-1.5 text-sm font-medium transition-colors ${
                  owner === t
                    ? "bg-blue-600 text-white"
                    : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                } disabled:opacity-50`}
              >
                {t === "ollama" ? "Ollama" : "Strata"}
              </button>
            ))}
          </div>
          <span className="text-xs text-slate-400 min-w-0">
            {owner === "strata"
              ? "Модели Ollama на GPU не запускаются; узел ollama-cpu (векторизация) работает как обычно."
              : "Strata выключена и не занимает ни видеопамять, ни ОЗУ."}
          </span>
        </div>

        {st.phase === "not_created" && (
          <div className="text-xs text-amber-200 bg-amber-950/40 border border-amber-900 rounded p-3">
            Контейнер Strata ещё не создан. Один раз выполните на сервере:
            <code className="block mt-1 break-all bg-slate-900 px-2 py-1 rounded text-slate-200">
              docker compose -f infra/docker-compose.yml -f
              infra/docker-compose.prod.yml --env-file infra/.env --profile
              strata up -d strata
            </code>
          </div>
        )}
        {st.down_while_owner && (
          <div className="text-xs text-red-200 bg-red-950/40 border border-red-900 rounded p-3">
            Видеокарта отдана Strata, но её контейнер не работает (сбой или
            перезагрузка). Слоты на Strata сейчас не отвечают — включите Strata
            снова или верните видеокарту Ollama.
          </div>
        )}
        {installOnly && (
          <div className="flex flex-wrap items-center gap-3 text-xs text-amber-200 bg-amber-950/40 border border-amber-900 rounded p-3">
            <span className="min-w-0">
              Скачивается {st.desired.model}
              {switchingTo?.size_on_disk_gb != null &&
                ` — ${switchingTo.size_on_disk_gb} из ~${switchingTo.disk_need_gb} ГБ`}
              . Видеокарта у Ollama, работа не прерывается.
            </span>
            <button
              className={btnSecondary}
              disabled={busy}
              onClick={() => void stopInstall()}
            >
              Остановить скачивание
            </button>
          </div>
        )}
        {st.docker_error && (
          <div className="text-xs text-red-300">
            Docker недоступен: {st.docker_error}
          </div>
        )}

        {/* Подтверждение переключения */}
        {confirm === "strata" && (
          <div className="rounded border border-blue-800 bg-blue-950/30 p-3 space-y-3">
            <div className="text-sm text-slate-100">
              Отдать видеокарту Strata ({st.desired.model}, контекст{" "}
              {ctxLabel(st.desired.context)}
              {st.desired.vision ? ", с картинками" : ""})
            </div>
            <ul className="text-xs text-slate-300 list-disc pl-5 space-y-0.5">
              <li>
                Модели Ollama будут выгружены с GPU, новые на GPU-Ollama не
                запустятся.
              </li>
              <li>
                Загрузка Strata — 1–3 минуты; на это время сервер может отвечать
                медленно.
              </li>
              <li>
                Strata отвечает на один запрос за раз: слоты, отданные ей,
                встают в общую очередь.
              </li>
              {needsDownload && switchingTo && (
                <li className="text-amber-200">
                  {switchingTo.model} ещё не скачана: первый запуск скачает ~
                  {Math.round(switchingTo.disk_need_gb)} ГБ места (свободно{" "}
                  {st.disk_free_gb?.toFixed(0) ?? "?"} ГБ).
                </li>
              )}
            </ul>
            {plan && (
              <div className="space-y-1">
                <div className="text-xs font-medium text-slate-300">
                  Слоты, которые переедут на Strata (вернутся при обратном
                  переключении):
                </div>
                {plan.map((p) => (
                  <label
                    key={p.slot}
                    className={`flex flex-wrap items-baseline gap-2 text-xs ${
                      p.move ? "text-slate-200" : "text-slate-500"
                    }`}
                  >
                    <input
                      type="checkbox"
                      disabled={!p.move}
                      checked={moveSlots.has(p.slot)}
                      onChange={(e) => {
                        const next = new Set(moveSlots);
                        if (e.target.checked) next.add(p.slot);
                        else next.delete(p.slot);
                        setMoveSlots(next);
                      }}
                    />
                    <span className="font-medium">{p.label}</span>
                    <span className="font-mono text-slate-500 break-all">
                      {p.current_model ?? "—"}
                    </span>
                    <span
                      className={
                        !p.move && p.reason.includes("перестанет")
                          ? "text-amber-300"
                          : ""
                      }
                    >
                      · {p.reason}
                    </span>
                  </label>
                ))}
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              <button
                className={btnPrimary}
                disabled={busy}
                onClick={() => void doSwitch()}
              >
                {busy ? "Переключаю…" : "Отдать видеокарту Strata"}
              </button>
              <button
                className={btnSecondary}
                disabled={busy}
                onClick={() => setConfirm(null)}
              >
                Отмена
              </button>
            </div>
          </div>
        )}
        {confirm === "ollama" && (
          <div className="rounded border border-blue-800 bg-blue-950/30 p-3 space-y-3">
            <div className="text-sm text-slate-100">
              Вернуть видеокарту Ollama
            </div>
            <div className="text-xs text-slate-300">
              Strata будет остановлена, видеопамять и ОЗУ освободятся. Запросы,
              которые она обрабатывает сейчас, оборвутся.
            </div>
            <label className="flex items-center gap-2 text-xs text-slate-200">
              <input
                type="checkbox"
                checked={restore}
                disabled={!st.switch_revision}
                onChange={(e) => setRestore(e.target.checked)}
              />
              {st.switch_revision
                ? "Вернуть слоты, перенесённые на Strata при переключении (кроме переназначенных вручную)"
                : "Слоты при переключении не переносились — возвращать нечего"}
            </label>
            <div className="flex flex-wrap gap-2">
              <button
                className={btnPrimary}
                disabled={busy}
                onClick={() => void doSwitch()}
              >
                {busy ? "Переключаю…" : "Вернуть Ollama"}
              </button>
              <button
                className={btnSecondary}
                disabled={busy}
                onClick={() => setConfirm(null)}
              >
                Отмена
              </button>
            </div>
          </div>
        )}

        {/* Состояние сервера */}
        {st.health && (
          <div className="text-xs text-slate-300 flex flex-wrap gap-x-4 gap-y-1">
            <span>
              модель <span className="font-mono">{st.health.model}</span>
            </span>
            <span>квантование {st.desired.model}</span>
            {st.health.max_context && (
              <span>контекст {ctxLabel(st.health.max_context)}</span>
            )}
            <span>картинки {st.health.images ? "да" : "нет"}</span>
          </div>
        )}
        {st.log_tail.length > 0 && (
          <pre className="text-[11px] leading-snug font-mono bg-slate-950 border border-slate-800 rounded p-2 max-h-48 overflow-auto whitespace-pre-wrap break-all text-slate-400">
            {st.log_tail.join("\n")}
          </pre>
        )}

        {/* Квантование и параметры */}
        {draft && (
          <div className="space-y-2">
            <div className="text-xs font-medium text-slate-300">
              Квантование
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {st.quants.map((q) => (
                <div
                  key={q.model}
                  className={`flex flex-wrap items-start gap-2 rounded border p-2 text-xs min-w-0 ${
                    draft.model === q.model
                      ? "border-blue-600 bg-blue-950/30"
                      : "border-slate-700 hover:border-slate-500"
                  }`}
                >
                  <label className="flex flex-1 items-start gap-2 cursor-pointer min-w-0">
                    <input
                      type="radio"
                      name="strata-quant"
                      className="mt-0.5"
                      checked={draft.model === q.model}
                      onChange={() => setDraft({ ...draft, model: q.model })}
                    />
                    <span className="min-w-0">
                      <span className="block text-slate-100 font-medium">
                        {q.label}
                        {q.installed && (
                          <span className="ml-2 text-emerald-300 font-normal">
                            скачана
                          </span>
                        )}
                        {st.desired.model === q.model && (
                          <span className="ml-2 text-blue-300 font-normal">
                            выбрана
                          </span>
                        )}
                      </span>
                      <span className="block text-slate-400">
                        {q.installed
                          ? `на диске ${q.size_on_disk_gb ?? "?"} ГБ`
                          : `скачать ~${q.download_gb} ГБ, займёт ~${Math.round(q.disk_need_gb)} ГБ`}{" "}
                        · эксперты {q.experts_gb} ГБ
                        {q.low_ram_mode && " · режим нехватки ОЗУ (медленнее)"}
                      </span>
                    </span>
                  </label>
                  {q.installed && st.desired.model !== q.model && (
                    <QuantDelete
                      model={q.model}
                      sizeGb={q.size_on_disk_gb}
                      onDeleted={(next) => setSt(next)}
                    />
                  )}
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-xs text-slate-300">
                Контекст
                <select
                  className={`${select} !w-auto`}
                  value={draft.context}
                  onChange={(e) =>
                    setDraft({ ...draft, context: Number(e.target.value) })
                  }
                >
                  {st.contexts.map((c) => (
                    <option key={c} value={c}>
                      {ctxLabel(c)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-2 text-xs text-slate-300">
                <input
                  type="checkbox"
                  checked={draft.vision}
                  onChange={(e) =>
                    setDraft({ ...draft, vision: e.target.checked })
                  }
                />
                Картинки (кодировщик изображений, ~1,2 ГБ видеопамяти)
              </label>
              <button
                className={btnPrimary}
                disabled={busy || !draftChanged}
                onClick={() => void applyConfig()}
              >
                Применить
              </button>
              {!st.installed.includes(st.desired.model) &&
                !draftChanged &&
                !installOnly && (
                  <button
                    className={btnSecondary}
                    disabled={
                      busy ||
                      st.container === "running" ||
                      st.phase === "not_created"
                    }
                    title={
                      st.container === "running"
                        ? "Strata запущена: верните видеокарту Ollama, затем скачивайте"
                        : "Скачать, не отдавая видеокарту"
                    }
                    onClick={() => void install()}
                  >
                    Скачать {st.desired.model} заранее
                  </button>
                )}
            </div>
            <div className="text-xs text-slate-500">
              Применяется перезапуском Strata (если она включена). Квантование,
              которого ещё нет на диске, скачивается при запуске
              {desiredQuant && !desiredQuant.installed
                ? ` (займёт ~${Math.round(desiredQuant.disk_need_gb)} ГБ)`
                : ""}
              ; уже скачанные переключаются без загрузки. ОЗУ сервера:{" "}
              {st.ram_total_gb?.toFixed(0) ?? "?"} ГБ, свободно на диске{" "}
              {st.disk_free_gb?.toFixed(0) ?? "?"} ГБ.
            </div>
          </div>
        )}

        <StrataResidency st={st} onStatus={setSt} />
        <StrataAccessBlock externalUrl={st.external_url} />
      </div>
    </div>
  );
}

const idleLabel = (sec: number) =>
  sec === 0 ? "никогда" : sec < 60 ? `${sec} с` : `${Math.round(sec / 60)} мин`;

/** Выгрузка после простоя — как keep_alive у Ollama, чтобы карта доставалась ComfyUI. */
function StrataResidency({
  st,
  onStatus,
}: {
  st: StrataStatus;
  onStatus: (s: StrataStatus) => void;
}) {
  const toast = useToast();
  const [idle, setIdle] = useState(st.runtime.idle_unload_s);
  const [freeComfy, setFreeComfy] = useState(st.runtime.free_comfyui);
  const [parallel, setParallel] = useState(st.runtime.parallel);
  const [cacheMib, setCacheMib] = useState(st.runtime.conversation_cache_mib);
  const [busy, setBusy] = useState(false);
  const changed =
    idle !== st.runtime.idle_unload_s ||
    freeComfy !== st.runtime.free_comfyui ||
    parallel !== st.runtime.parallel ||
    cacheMib !== st.runtime.conversation_cache_mib;

  const apply = async () => {
    setBusy(true);
    try {
      const r = await strataRuntime({
        idle_unload_s: idle,
        free_comfyui: freeComfy,
        parallel,
        conversation_cache_mib: cacheMib,
      });
      onStatus(r.status);
      toast.ok(
        "Настройки работы Strata сохранены",
        r.restarted
          ? "Strata перезапускается"
          : "Применится при следующем включении",
      );
    } catch (e) {
      toast.error("Не удалось сохранить", String((e as Error).message ?? e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 border-t border-slate-800 pt-3">
      <div className="text-xs font-medium text-slate-300">Работа модели</div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-slate-300">
          Выгружать модель после простоя
          <select
            className={`${select} !w-auto`}
            value={idle}
            onChange={(e) => setIdle(Number(e.target.value))}
          >
            {st.idle_choices.map((c) => (
              <option key={c} value={c}>
                {idleLabel(c)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-xs text-slate-300">
          <input
            type="checkbox"
            checked={freeComfy}
            disabled={idle === 0}
            onChange={(e) => setFreeComfy(e.target.checked)}
          />
          Перед загрузкой просить ComfyUI освободить видеопамять
        </label>
        <label className="flex items-center gap-2 text-xs text-slate-300">
          Параллельные запросы
          <select
            className={`${select} !w-auto`}
            value={parallel}
            onChange={(e) => setParallel(Number(e.target.value))}
          >
            {st.parallel_choices.map((c) => (
              <option key={c} value={c}>
                {c === 1 ? "1 — по очереди" : `${c} одновременно`}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-xs text-slate-300">
          Кэш разговоров
          <select
            className={`${select} !w-auto`}
            value={cacheMib}
            onChange={(e) => setCacheMib(Number(e.target.value))}
          >
            {st.conversation_cache_choices.map((c) => (
              <option key={c} value={c}>
                {c === 0 ? "выключен" : `${c / 1024} ГБ ОЗУ`}
              </option>
            ))}
          </select>
        </label>
        <button
          className={btnPrimary}
          disabled={busy || !changed}
          onClick={() => void apply()}
        >
          Применить
        </button>
      </div>
      <div className="text-xs text-slate-500">
        Выгруженная модель загружается обратно при первом запросе (~30 с), ответ
        на него приходит позже. Задачи Студии сами выгружают Strata перед
        запуском. Если видеопамяти меньше{" "}
        {Math.round(st.runtime.min_free_vram_mib / 1024)} ГБ, Strata не
        загружается и отвечает «видеокарта занята другой программой». Два
        параллельных запроса: короткие задачи не ждут, пока читается длинный
        документ, суммарно на ~20% быстрее, но на видеокарте ~9% меньше
        экспертов. Кэш разговоров хранит до 4 разговоров в ОЗУ: реплика агента
        после чужого запроса не перечитывает длинную историю (0,5 с вместо 28 с
        на 60 тыс. токенов).
      </div>
    </div>
  );
}

/** Адрес и ключ для подключения с другого компьютера в сети. */
function StrataAccessBlock({ externalUrl }: { externalUrl: string | null }) {
  const toast = useToast();
  const [access, setAccess] = useState<StrataAccess | null>(null);

  const reveal = async () => {
    try {
      setAccess(await strataAccess());
    } catch (e) {
      toast.error(
        "Не удалось получить ключ",
        String((e as Error).message ?? e),
      );
    }
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.ok("Скопировано");
    } catch {
      toast.error("Буфер обмена недоступен", text);
    }
  };

  if (!externalUrl) {
    return (
      <div className="border-t border-slate-800 pt-3 text-xs text-slate-500">
        Внешний доступ не настроен: задайте STRATA_PUBLIC_URL в infra/.env.
      </div>
    );
  }
  const base = `${externalUrl.replace(/\/$/, "")}/v1`;
  return (
    <div className="space-y-2 border-t border-slate-800 pt-3 text-xs">
      <div className="font-medium text-slate-300">
        Доступ с другого компьютера в сети
      </div>
      <div className="flex flex-wrap items-center gap-2 text-slate-300">
        <span>OpenAI-совместимый адрес</span>
        <code className="bg-slate-900 px-2 py-0.5 rounded break-all">
          {base}
        </code>
        <button className={btnSecondary} onClick={() => void copy(base)}>
          Копировать
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-slate-300">
        <span>Ключ API</span>
        {access?.api_key ? (
          <>
            <code className="bg-slate-900 px-2 py-0.5 rounded break-all">
              {access.api_key}
            </code>
            <button
              className={btnSecondary}
              onClick={() => void copy(access.api_key ?? "")}
            >
              Копировать
            </button>
          </>
        ) : (
          <button className={btnSecondary} onClick={() => void reveal()}>
            Показать ключ
          </button>
        )}
      </div>
      <div className="text-slate-500">
        Работает, пока видеокарта отдана Strata. Имя модели — любое. Веб-чат
        Strata: <code className="bg-slate-900 px-1 rounded">{externalUrl}</code>
        . Ключ обязателен: без него сервер не отвечает.
      </div>
    </div>
  );
}

/** Удаление скачанного квантования: два шага, потому что вернуть его — это
 * повторная загрузка десятков гигабайт. Выбранное квантование не удаляется
 * (кнопки у него нет, сервер тоже откажет). */
function QuantDelete({
  model,
  sizeGb,
  onDeleted,
}: {
  model: string;
  sizeGb: number | null;
  onDeleted: (s: StrataStatus) => void;
}) {
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const remove = async () => {
    setBusy(true);
    try {
      const r = await strataDeleteQuant(model);
      onDeleted(r.status);
      toast.ok(`${model} удалена`, `Освобождено ~${Math.round(r.freed_gb)} ГБ`);
    } catch (e) {
      toast.error("Не удалось удалить", String((e as Error).message ?? e));
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  if (!confirming) {
    return (
      <button
        type="button"
        className="text-xs text-red-400 hover:text-red-300"
        aria-label={`Удалить ${model}`}
        onClick={() => setConfirming(true)}
      >
        Удалить
      </button>
    );
  }
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="text-red-300">
        Удалить {model} с диска{sizeGb != null ? ` (${sizeGb} ГБ)` : ""}?
      </span>
      <button
        type="button"
        className={`${btn} bg-red-700 hover:bg-red-600 text-white disabled:opacity-50`}
        disabled={busy}
        onClick={() => void remove()}
      >
        {busy ? "Удаляю…" : "Да, удалить"}
      </button>
      <button
        type="button"
        className={btnSecondary}
        disabled={busy}
        onClick={() => setConfirming(false)}
      >
        Отмена
      </button>
    </span>
  );
}
