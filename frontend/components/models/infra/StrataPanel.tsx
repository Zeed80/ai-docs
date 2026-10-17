"use client";

/**
 * Видеокарта: Ollama ⇄ Strata.
 *
 * Strata держит одну модель (Qwen3.8-Flash-Next 125B, Qwen3.6-35B и др.) на
 * GPU и в оперативной памяти и занимает карту целиком, поэтому рядом с
 * GPU-Ollama не работает: оператор переключает видеокарту между ними.
 * Переключение — явное решение, и оно может переназначить слоты; что именно
 * переедет, показывается до нажатия.
 *
 * Здесь же: установка, обновление и удаление самой Strata (сборка образа
 * нового выпуска, пересоздание контейнера), скачивание моделей рядом с
 * работающей Strata и быстрая смена загруженной модели, запас ОЗУ системе.
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
  strataAccess,
  strataCheckUpdate,
  strataConfig,
  strataDeleteQuant,
  strataDismissJob,
  strataInstall,
  strataInstallServer,
  strataLoad,
  strataLoadPlan,
  strataRetryApply,
  strataRuntime,
  strataSlotPlan,
  strataStatus,
  strataStopInstall,
  strataSwitch,
  strataUninstall,
  strataUpdate,
} from "@/lib/models/api";
import type {
  StrataAccess,
  StrataLoadPlan,
  StrataPhase,
  StrataQuant,
  StrataRamMode,
  StrataSlotPlanItem,
  StrataStatus,
} from "@/lib/models/types";

const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200 disabled:opacity-50`;

const PHASE: Record<StrataPhase, { text: string; cls: string }> = {
  not_created: {
    text: "не установлена",
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
  // Контекст и картинки: для выбранной модели (Применить) и для новых скачиваний.
  const [params, setParams] = useState<{
    context: number;
    vision: boolean;
  } | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await strataStatus();
      setSt(s);
      setLoadError(null);
      setParams(
        (d) => d ?? { context: s.desired.context, vision: s.desired.vision },
      );
    } catch (e) {
      setLoadError(String((e as Error).message ?? e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Пока идёт установка/загрузка/сборка — опрашиваем часто, иначе редко.
  const active =
    st?.phase === "installing" ||
    st?.phase === "loading" ||
    st?.installer?.stage === "downloading" ||
    st?.server?.job?.stage === "building" ||
    st?.server?.job?.stage === "built";
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

  // «Скачивание заранее» прежней схемы шло в самом контейнере Strata.
  const stopLegacyInstall = async () => {
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

  const applyParams = async () => {
    if (!params || !st) return;
    setBusy(true);
    try {
      const r = await strataConfig({ model: st.desired.id, ...params });
      setSt(r.status);
      setParams({
        context: r.status.desired.context,
        vision: r.status.desired.vision,
      });
      toast.ok(
        "Настройки Strata сохранены",
        r.restarted
          ? "Strata перенастраивается и перезапускается (без новой загрузки файлов)"
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
  const current = st.quants.find((q) => q.id === st.desired.id);
  const recorded = current?.installed_settings;
  const paramsChanged =
    !!params &&
    (params.context !== (recorded?.context ?? st.desired.context) ||
      params.vision !== (recorded?.vision ?? st.desired.vision));
  const needsDownload = !!current && !current.installed;
  const notCreated = st.phase === "not_created";

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
        <StrataServerBlock st={st} onStatus={setSt} />

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
                disabled={busy || notCreated || installOnly}
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
              Скачивается {st.desired.id}
              {current?.size_on_disk_gb != null &&
                ` — ${current.size_on_disk_gb} из ~${current.disk_need_gb} ГБ`}
              . Видеокарта у Ollama, работа не прерывается.
            </span>
            <button
              className={btnSecondary}
              disabled={busy}
              onClick={() => void stopLegacyInstall()}
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
              Отдать видеокарту Strata ({familyTitle(st, st.desired.family)}{" "}
              {st.desired.model}, контекст {ctxLabel(st.desired.context)}
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
              {needsDownload && current && (
                <li className="text-amber-200">
                  {current.id} ещё не скачана: первый запуск скачает её и займёт
                  ~{Math.round(current.disk_need_gb)} ГБ (свободно{" "}
                  {st.disk_free_gb?.toFixed(0) ?? "?"} ГБ). Быстрее — скачать
                  заранее кнопкой «Скачать» ниже.
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

        {/* Модели: семейства и размеры */}
        {!notCreated && (
          <StrataModels st={st} params={params} onStatus={setSt} />
        )}

        {/* Параметры выбранной модели (и новых скачиваний) */}
        {params && !notCreated && (
          <div className="space-y-2 border-t border-slate-800 pt-3">
            <div className="text-xs font-medium text-slate-300">
              Параметры модели
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <label className="flex items-center gap-2 text-xs text-slate-300">
                Контекст
                <select
                  className={`${select} !w-auto`}
                  value={params.context}
                  onChange={(e) =>
                    setParams({ ...params, context: Number(e.target.value) })
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
                  checked={params.vision}
                  onChange={(e) =>
                    setParams({ ...params, vision: e.target.checked })
                  }
                />
                Картинки (кодировщик изображений, ~1,2 ГБ видеопамяти)
              </label>
              <button
                className={btnPrimary}
                disabled={busy || !paramsChanged}
                onClick={() => void applyParams()}
              >
                Применить к {st.desired.id}
              </button>
            </div>
            <div className="text-xs text-slate-500">
              Контекст и картинки записываются в настройку модели при её
              установке: смена — это перенастройка Strata (несколько минут, без
              новой загрузки файлов). Новые скачивания берут эти же значения;
              уже скачанные модели загружаются со своими. ОЗУ сервера:{" "}
              {st.ram_total_gb?.toFixed(0) ?? "?"} ГБ, свободно на диске{" "}
              {st.disk_free_gb?.toFixed(0) ?? "?"} ГБ.
            </div>
          </div>
        )}

        {!notCreated && <StrataResidency st={st} onStatus={setSt} />}
        {!notCreated && <StrataMemory st={st} onStatus={setSt} />}
        {!notCreated && <StrataAccessBlock externalUrl={st.external_url} />}
      </div>
    </div>
  );
}

const familyTitle = (st: StrataStatus, family: string) =>
  st.families.find((f) => f.family === family)?.title ?? family;

const errText = (e: unknown) => String((e as Error).message ?? e);

/** «0.1.43.1» < «0.1.45»; неизвестная версия ничего не запрещает. */
const olderThan = (version: string | null | undefined, need: string | null) => {
  if (!version || !need) return false;
  const a = version.split(".").map(Number);
  const b = need.split(".").map(Number);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] ?? 0) - (b[i] ?? 0);
    if (d !== 0) return d < 0;
  }
  return false;
};

const RAM_MODE: Record<StrataRamMode, string> = {
  auto: "",
  off: "все эксперты в ОЗУ",
  resident: "режим нехватки ОЗУ: часть экспертов на видеокарте (медленнее)",
  mmap: "эксперты читаются через файловый кэш (заметно медленнее)",
  budget: "часть экспертов читается с SSD",
  own: "почти целиком на видеокарте",
};

/** Версия Strata, обновление до нового выпуска, установка с нуля и удаление. */
function StrataServerBlock({
  st,
  onStatus,
}: {
  st: StrataStatus;
  onStatus: (s: StrataStatus) => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [notes, setNotes] = useState(false);
  const [uninstall, setUninstall] = useState(false);
  const [wipe, setWipe] = useState(false);
  const server = st.server ?? {
    current: null,
    latest: {},
    update_available: false,
    job: null,
  };
  const job = server.job;
  const latest = server.latest ?? {};
  const notCreated = st.phase === "not_created";
  const modelsGb = st.quants
    .filter((q) => q.installed)
    .reduce((sum, q) => sum + (q.size_on_disk_gb ?? 0), 0);

  const run = async (
    fn: () => Promise<{ status: StrataStatus }>,
    ok: [string, string?],
    fail: string,
  ) => {
    setBusy(true);
    try {
      const r = await fn();
      onStatus(r.status);
      toast.ok(...ok);
      return true;
    } catch (e) {
      toast.error(fail, errText(e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const check = async () => {
    setBusy(true);
    try {
      const info = await strataCheckUpdate();
      onStatus({ ...st, server: info });
      if (info.latest?.error)
        toast.error("Проверка не удалась", info.latest.error);
      else
        toast.ok(
          info.update_available
            ? `Доступна Strata ${info.latest.version}`
            : "Установлена последняя версия",
        );
    } catch (e) {
      toast.error("Проверка не удалась", errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      {notCreated && !job && (
        <div className="space-y-2 text-xs text-amber-200 bg-amber-950/40 border border-amber-900 rounded p-3">
          <div className="text-sm text-slate-100">Strata не установлена</div>
          <div>
            Установка соберёт образ последнего выпуска
            {latest.version ? ` (${latest.version})` : ""} под эту видеокарту —
            20–40 минут, работа не прерывается — и создаст контейнер. Затем
            скачайте модель в списке ниже и отдайте Strata видеокарту.
          </div>
          <button
            className={btnPrimary}
            disabled={busy || !!st.docker_error}
            onClick={() =>
              void run(
                strataInstallServer,
                ["Установка Strata началась", "Сборка образа идёт в фоне"],
                "Установка не запущена",
              )
            }
          >
            Установить Strata
          </button>
        </div>
      )}

      {!notCreated && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-xs text-slate-300">
          <span>
            Strata{" "}
            <span className="font-mono">
              {server.current?.version ?? "версия неизвестна"}
            </span>
          </span>
          {server.update_available && !job ? (
            <>
              <span className="px-2 py-0.5 rounded bg-emerald-900/60 text-emerald-200">
                доступна {latest.version}
              </span>
              <button
                className={btnPrimary}
                disabled={busy}
                title="Соберёт образ нового выпуска (20–40 мин, в фоне) и перезапустит Strata на нём; скачанные модели остаются"
                aria-label={`Обновить Strata до ${latest.version}`}
                onClick={() =>
                  void run(
                    strataUpdate,
                    [
                      `Обновление до ${latest.version} началось`,
                      "Образ собирается в фоне; Strata перезапустится, когда он будет готов",
                    ],
                    "Обновление не запущено",
                  )
                }
              >
                Обновить
              </button>
              {latest.notes && (
                <button
                  type="button"
                  className="text-blue-300 hover:text-blue-200"
                  aria-expanded={notes}
                  onClick={() => setNotes(!notes)}
                >
                  {notes ? "Скрыть, что нового" : "Что нового"}
                </button>
              )}
            </>
          ) : (
            !job && (
              <>
                <span className="text-slate-500">
                  {latest.error
                    ? "не удалось проверить обновления"
                    : latest.version
                      ? "последняя версия"
                      : ""}
                </span>
                <button
                  type="button"
                  className="text-blue-300 hover:text-blue-200 disabled:opacity-50"
                  disabled={busy}
                  onClick={() => void check()}
                >
                  Проверить обновления
                </button>
              </>
            )
          )}
          <button
            type="button"
            className="ml-auto text-red-400 hover:text-red-300"
            onClick={() => setUninstall(!uninstall)}
          >
            Удалить Strata…
          </button>
        </div>
      )}
      {notes && latest.notes && (
        <pre className="text-[11px] leading-snug bg-slate-950 border border-slate-800 rounded p-2 max-h-56 overflow-auto whitespace-pre-wrap break-words text-slate-300">
          {latest.notes}
        </pre>
      )}

      {job && (
        <div className="space-y-2 rounded border border-slate-700 p-3 text-xs">
          <div className="text-slate-200">
            {job.stage === "building" &&
              `Собирается образ Strata ${job.version ?? ""} — ${
                job.then === "create" ? "установка" : "обновление"
              }. Работа не прерывается; по готовности Strata ${
                job.then === "create"
                  ? "будет создана"
                  : "перезапустится на новой версии"
              }.`}
            {job.stage === "built" &&
              "Образ собран, Strata переводится на него…"}
            {job.stage === "failed" &&
              `Сборка ${job.version ?? ""} не удалась (код ${job.exit_code}). Strata работает на прежней версии.`}
            {job.stage === "apply_failed" &&
              `Образ ${job.version ?? ""} собран, но перевести Strata на него не удалось: ${job.error}`}
          </div>
          {job.log_tail.length > 0 && (
            <pre className="text-[11px] leading-snug font-mono bg-slate-950 border border-slate-800 rounded p-2 max-h-40 overflow-auto whitespace-pre-wrap break-all text-slate-400">
              {job.log_tail.join("\n")}
            </pre>
          )}
          <div className="flex flex-wrap gap-2">
            {job.stage === "apply_failed" && (
              <button
                className={btnPrimary}
                disabled={busy}
                onClick={() =>
                  void run(strataRetryApply, ["Повторяю"], "Не удалось")
                }
              >
                Применить ещё раз
              </button>
            )}
            {job.stage === "failed" && (
              <button
                className={btnPrimary}
                disabled={busy}
                onClick={() =>
                  void run(
                    notCreated ? strataInstallServer : strataUpdate,
                    ["Сборка запущена заново"],
                    "Не удалось запустить",
                  )
                }
              >
                Собрать заново
              </button>
            )}
            <button
              className={btnSecondary}
              disabled={busy}
              onClick={() =>
                void run(
                  strataDismissJob,
                  [job.stage === "building" ? "Сборка остановлена" : "Скрыто"],
                  "Не удалось",
                )
              }
            >
              {job.stage === "building" ? "Остановить сборку" : "Скрыть"}
            </button>
          </div>
        </div>
      )}

      {uninstall && (
        <div className="space-y-2 rounded border border-red-900 bg-red-950/30 p-3 text-xs text-red-100">
          <div className="text-sm">Удалить Strata с сервера?</div>
          <div>
            Контейнер и образы Strata будут удалены; если видеокарта у Strata,
            она вернётся Ollama вместе с перенесёнными слотами.
          </div>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={wipe}
              onChange={(e) => setWipe(e.target.checked)}
            />
            Удалить и скачанные модели (~{Math.round(modelsGb)} ГБ) — иначе
            повторная установка найдёт их и не будет качать заново
          </label>
          <div className="flex flex-wrap gap-2">
            <button
              className={`${btn} bg-red-700 hover:bg-red-600 text-white disabled:opacity-50`}
              disabled={busy}
              onClick={async () => {
                const ok = await run(
                  () => strataUninstall(wipe),
                  ["Strata удалена"],
                  "Не удалось удалить",
                );
                if (ok) setUninstall(false);
              }}
            >
              {busy
                ? "Удаляю…"
                : wipe
                  ? "Удалить вместе с моделями"
                  : "Удалить Strata"}
            </button>
            <button
              className={btnSecondary}
              disabled={busy}
              onClick={() => setUninstall(false)}
            >
              Отмена
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Модели Strata по семействам. Скачанная — «Загрузить» (старая выгружается,
 * слоты на Strata переходят на новую одной ревизией), нескачанная —
 * «Скачать» рядом с работающей Strata, без простоя.
 */
function StrataModels({
  st,
  params,
  onStatus,
}: {
  st: StrataStatus;
  params: { context: number; vision: boolean } | null;
  onStatus: (s: StrataStatus) => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [loadPlan, setLoadPlan] = useState<StrataLoadPlan | null>(null);
  // Какая версия модели показана в блоке (по умолчанию — загруженная).
  const [versionOf, setVersionOf] = useState<Record<string, string>>({});
  const installer = st.installer;
  const downloading =
    installer?.stage === "downloading" ? installer.variant : null;

  const openLoad = async (q: StrataQuant) => {
    try {
      setLoadPlan(await strataLoadPlan(q.id));
    } catch (e) {
      toast.error("Не удалось подготовить смену модели", errText(e));
    }
  };

  const load = async () => {
    if (!loadPlan) return;
    setBusy(true);
    try {
      const r = await strataLoad(loadPlan.id);
      onStatus(r.status);
      setLoadPlan(null);
      toast.ok(
        `Выбрана ${loadPlan.id}`,
        (r.restarted
          ? r.setup_pass
            ? "Strata перенастраивается и загружает её (несколько минут)"
            : "Strata загружает её (1–3 мин)"
          : "Загрузится при следующем включении Strata") +
          (r.moved.length ? `; слотов перешло: ${r.moved.length}` : ""),
      );
    } catch (e) {
      toast.error("Модель не загружена", errText(e));
    } finally {
      setBusy(false);
    }
  };

  const download = async (q: StrataQuant) => {
    setBusy(true);
    try {
      const r = await strataInstall({
        id: q.id,
        context: params?.context,
        vision: params?.vision,
      });
      onStatus(r.status);
      toast.ok(
        `Скачивание ${q.id} началось`,
        "Strata продолжает работать; прерванная загрузка продолжится с места остановки",
      );
    } catch (e) {
      toast.error("Скачивание не запущено", errText(e));
    } finally {
      setBusy(false);
    }
  };

  const stopDownload = async () => {
    setBusy(true);
    try {
      const r = await strataStopInstall();
      onStatus(r.status);
      toast.ok("Скачивание остановлено", "Повторный запуск продолжит");
    } catch (e) {
      toast.error("Не удалось остановить", errText(e));
    } finally {
      setBusy(false);
    }
  };

  const running = st.container === "running";

  const QuantRow = ({ q }: { q: StrataQuant }) => {
    const selected = q.id === st.desired.id;
    const isDownloading = downloading === q.id;
    const tooOld = olderThan(st.server?.current?.version, q.min_version);
    return (
      <div
        className={`flex flex-wrap items-start gap-2 rounded border p-2 text-xs min-w-0 ${
          selected ? "border-blue-600 bg-blue-950/30" : "border-slate-700"
        }`}
      >
        <span className="flex-1 min-w-0 space-y-0.5">
          <span className="block text-slate-100">
            <span className="font-medium font-mono">{q.label}</span>
            <span className="ml-2 text-slate-400">{q.bits}</span>
            {q.badge && (
              <span className="ml-2 px-1.5 rounded bg-slate-700 text-slate-200">
                {q.badge}
              </span>
            )}
            {selected && (
              <span className="ml-2 text-blue-300">
                {running ? "загружена" : "выбрана"}
              </span>
            )}
            {q.installed && !selected && (
              <span className="ml-2 text-emerald-300">скачана</span>
            )}
          </span>
          <span className="block text-slate-300">{q.about}</span>
          <span className="block text-slate-500">
            {isDownloading
              ? `скачивается: ${installer?.size_on_disk_gb ?? 0} из ~${Math.round(q.disk_need_gb)} ГБ`
              : q.installed
                ? `на диске ${q.size_on_disk_gb ?? "?"} ГБ`
                : `скачать ~${q.download_gb} ГБ, займёт ~${Math.round(q.disk_need_gb)} ГБ`}
            {" · "}ОЗУ ~{q.ram_gb} ГБ
            {RAM_MODE[q.ram_mode] && ` · ${RAM_MODE[q.ram_mode]}`}
            {q.family === "unsloth" && ` · файлы ${q.source}`}
          </span>
          {tooOld && (
            <span className="block text-amber-300">
              нужна Strata {q.min_version} или новее — сначала обновите Strata
            </span>
          )}
          {!q.fits_ram && (
            <span className="block text-amber-300">
              не помещается в память, оставленную Strata
            </span>
          )}
        </span>
        <span className="flex flex-wrap items-center gap-2">
          {q.installed && !selected && (
            <button
              type="button"
              className={btnSecondary}
              disabled={busy || !!loadPlan || tooOld}
              aria-label={`Загрузить ${q.id}`}
              onClick={() => void openLoad(q)}
            >
              Загрузить
            </button>
          )}
          {!q.installed && !isDownloading && (
            <button
              type="button"
              className={btnSecondary}
              disabled={busy || !!downloading || tooOld}
              title={
                downloading
                  ? `Сейчас скачивается ${downloading}`
                  : "Скачать рядом с работающей Strata, без простоя"
              }
              aria-label={`Скачать ${q.id}`}
              onClick={() => void download(q)}
            >
              Скачать
            </button>
          )}
          {isDownloading && (
            <button
              type="button"
              className={btnSecondary}
              disabled={busy}
              onClick={() => void stopDownload()}
            >
              Остановить
            </button>
          )}
          {q.installed && !selected && (
            <QuantDelete
              model={q.id}
              sizeGb={q.size_on_disk_gb}
              onDeleted={onStatus}
            />
          )}
        </span>
      </div>
    );
  };

  return (
    <div className="space-y-3">
      <div className="text-xs font-medium text-slate-300">Модели</div>

      {installer?.stage === "failed" && (
        <div className="space-y-2 rounded border border-red-900 bg-red-950/30 p-3 text-xs text-red-100">
          <div>
            Скачивание {installer.variant} прервалось (код {installer.exit_code}
            ). Повторный запуск продолжит с места остановки.
          </div>
          {installer.log_tail.length > 0 && (
            <pre className="text-[11px] leading-snug font-mono bg-slate-950 border border-slate-800 rounded p-2 max-h-40 overflow-auto whitespace-pre-wrap break-all text-slate-400">
              {installer.log_tail.join("\n")}
            </pre>
          )}
          <button
            className={btnSecondary}
            disabled={busy}
            onClick={() => void stopDownload()}
          >
            Закрыть
          </button>
        </div>
      )}

      {loadPlan && (
        <div className="rounded border border-blue-800 bg-blue-950/30 p-3 space-y-2 text-xs text-slate-300">
          <div className="text-sm text-slate-100">
            {running ? "Выгрузить" : "Вместо"} {st.desired.id}{" "}
            {running ? "и загрузить" : "выбрать"} {loadPlan.id}
          </div>
          <ul className="list-disc pl-5 space-y-0.5">
            <li>
              Контекст {ctxLabel(loadPlan.context)}
              {loadPlan.vision ? ", с картинками" : ", без картинок"} — как
              модель была установлена.
            </li>
            {running && (
              <li>
                Strata перезапустится:{" "}
                {loadPlan.setup_pass
                  ? "сначала перенастройка под запас ОЗУ (несколько минут), потом "
                  : ""}
                загрузка 1–3 минуты, запросы в это время ждут.
              </li>
            )}
            {loadPlan.follow.length > 0 && (
              <li>
                Перейдут на {loadPlan.catalog_key}:{" "}
                {loadPlan.follow.map((f) => f.label).join(", ")}
              </li>
            )}
            {loadPlan.warnings.map((w) => (
              <li key={w} className="text-amber-300">
                {w}
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            <button
              className={btnPrimary}
              disabled={busy}
              onClick={() => void load()}
            >
              {busy ? "Загружаю…" : "Да, загрузить"}
            </button>
            <button
              className={btnSecondary}
              disabled={busy}
              onClick={() => setLoadPlan(null)}
            >
              Отмена
            </button>
          </div>
        </div>
      )}

      {st.groups.map((group) => {
        const items = st.quants.filter((q) => q.group === group.id);
        if (!items.length) return null;
        const loaded = items.find((q) => q.id === st.desired.id);
        const version =
          versionOf[group.id] ?? loaded?.version ?? group.versions[0].id;
        const ver =
          group.versions.find((v) => v.id === version) ?? group.versions[0];
        const sizes = items.filter((q) => q.version === ver.id);
        return (
          <section
            key={group.id}
            aria-label={group.title}
            className="space-y-2 rounded border border-slate-700 p-3"
          >
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <h3 className="text-sm font-medium text-slate-100">
                {group.title}
              </h3>
              {loaded && (
                <span className="text-xs text-blue-300">
                  {running ? "загружена" : "выбрана"}:{" "}
                  {loaded.version !== "original"
                    ? `${group.versions.find((v) => v.id === loaded.version)?.title} `
                    : ""}
                  {loaded.model}
                </span>
              )}
            </div>
            <div className="text-xs text-slate-300">{group.summary}</div>
            <ul className="text-xs text-slate-400 list-disc pl-5 space-y-0.5">
              {group.facts.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
            {group.versions.length > 1 && (
              <div
                role="radiogroup"
                aria-label={`Версия ${group.title}`}
                className="inline-flex flex-wrap rounded border border-slate-600 overflow-hidden"
              >
                {group.versions.map((v) => {
                  const has = items.some(
                    (q) => q.version === v.id && q.installed,
                  );
                  return (
                    <button
                      key={v.id}
                      type="button"
                      role="radio"
                      aria-checked={v.id === ver.id}
                      onClick={() =>
                        setVersionOf({ ...versionOf, [group.id]: v.id })
                      }
                      className={`px-3 py-1 text-xs font-medium transition-colors ${
                        v.id === ver.id
                          ? "bg-blue-600 text-white"
                          : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                      }`}
                    >
                      {v.title}
                      {has && (
                        <span
                          className="ml-1 text-emerald-300"
                          aria-label="есть скачанные"
                        >
                          ●
                        </span>
                      )}
                    </button>
                  );
                })}
              </div>
            )}
            <div className="text-xs text-slate-400">{ver.about}</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {sizes.map((q) => (
                <QuantRow key={q.id} q={q} />
              ))}
            </div>
          </section>
        );
      })}
      {installer?.stage === "downloading" && installer.log_tail.length > 0 && (
        <pre className="text-[11px] leading-snug font-mono bg-slate-950 border border-slate-800 rounded p-2 max-h-32 overflow-auto whitespace-pre-wrap break-all text-slate-400">
          {installer.log_tail.join("\n")}
        </pre>
      )}
    </div>
  );
}

/** Сколько оперативной памяти Strata оставляет системе. */
function StrataMemory({
  st,
  onStatus,
}: {
  st: StrataStatus;
  onStatus: (s: StrataStatus) => void;
}) {
  const toast = useToast();
  const [reserve, setReserve] = useState(st.runtime.ram_reserve_gb ?? 0);
  const [busy, setBusy] = useState(false);
  const total = st.ram_total_gb ?? 0;
  const plan = st.ram_plan;

  const apply = async () => {
    setBusy(true);
    try {
      const r = await strataRuntime({ ram_reserve_gb: reserve });
      onStatus(r.status);
      toast.ok(
        reserve
          ? `Strata оставит системе ${reserve} ГБ`
          : "Ограничение памяти снято",
        r.restarted
          ? r.setup_pass
            ? "Strata перенастраивает размещение экспертов и перезапускается"
            : "Strata перезапускается с новым ограничением"
          : "Применится при следующем включении",
      );
    } catch (e) {
      toast.error("Не удалось сохранить", errText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 border-t border-slate-800 pt-3">
      <div className="text-xs font-medium text-slate-300">
        Оперативная память
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <label className="flex items-center gap-2 text-xs text-slate-300">
          Оставлять системе
          <select
            className={`${select} !w-auto`}
            value={reserve}
            onChange={(e) => setReserve(Number(e.target.value))}
          >
            {st.reserve_choices.map((c) => (
              <option key={c} value={c} disabled={!!c && total - c < 24}>
                {c === 0 ? "без ограничения" : `${c} ГБ`}
              </option>
            ))}
          </select>
        </label>
        <button
          className={btnPrimary}
          disabled={busy || reserve === (st.runtime.ram_reserve_gb ?? 0)}
          onClick={() => void apply()}
        >
          Применить
        </button>
      </div>
      <div className="text-xs text-slate-500">
        {plan.limit_gb
          ? `Strata может занять не больше ${plan.limit_gb} ГБ из ${total.toFixed(0)} (жёсткий предел контейнера). `
          : `Strata берёт память по своим правилам (у ${st.desired.id} — около ${
              st.quants.find((q) => q.id === st.desired.id)?.ram_gb ?? "?"
            } ГБ). `}
        {RAM_MODE[plan.mode] && `${st.desired.id}: ${RAM_MODE[plan.mode]}. `}
        Чем больше запас, тем меньше экспертов в ОЗУ: модель переходит в режим
        нехватки памяти и отвечает медленнее (для IQ3_XXS через файловый кэш —
        примерно вдвое). Смена режима — перенастройка Strata без новой загрузки
        файлов, режиму нехватки ОЗУ может понадобиться место на диске под копию
        экспертов.
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
