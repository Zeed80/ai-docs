"use client";

/**
 * Назначение моделей на слоты — главный экран раздела.
 *
 * Последняя вкладка, вынесенная из монолита. Типы слота, каталожной записи и
 * элементов черновика едут вместе с ней: за её пределами они не нужны.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useToast } from "@/components/ui/primitives/Toast";
import { DiffPanel } from "@/components/models/assignment/DiffPanel";
import { RevisionHistory } from "@/components/models/assignment/RevisionHistory";
import {
  SlotHealthStrip,
  type SlotHealth,
} from "@/components/models/assignment/SlotHealthStrip";
import { SlotThinkingControl } from "@/components/models/assignment/SlotThinkingControl";
import { ProviderModelPicker } from "@/components/models/picker/ProviderModelPicker";
import { RoutingChains } from "@/components/models/telemetry/RoutingChains";
import {
  btn,
  card,
  cardHeader as cardH,
  input,
  select,
} from "@/components/ui/primitives/tokens";
import { detailText } from "@/lib/models/format";
import { providerLabel } from "@/lib/models/labels";
import type {
  CatalogModel,
  Modality,
  ProviderInstance as ProviderInstanceT,
  ThinkingLevel,
} from "@/lib/models/types";

const API = getApiBaseUrl();
const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200`;

const LOCAL_PROVIDERS = [
  "ollama",
  "llamacpp",
  "vllm",
  "openai_compatible",
  "lmstudio",
];

const THINKING_DISABLE_SUPPORTED_PROVIDERS = [
  "ollama",
  "llamacpp",
  "vllm",
  "openrouter",
  "ollama_cloud",
  "openai",
  "groq",
  "xai",
  "dashscope",
  "qwen",
  "cerebras",
];

const THINKING_LEVEL_LABEL: Record<string, string> = {
  low: "низкая",
  medium: "средняя",
  high: "высокая",
};

const GROUP_ICON: Record<string, string> = {
  Документы: "📄",
  Агент: "🤖",
  Поиск: "🔎",
  Оцифровка: "📐",
};

interface CatalogEntry {
  key: string;
  provider: string;
  provider_model: string;
  modalities: string[];
  local_only: boolean;
  vram_gb_estimate: number | null;
  status: string;
  // Приходят из /live-models и /models; до этого не запрашивались и не
  // показывались, хотя лежат в каталоге моделей с самого начала.
  max_context_tokens?: number | null;
  supports_tool_calling?: boolean;
  supports_structured_output?: boolean;
  cost_per_1k_input?: number | null;
  cost_per_1k_output?: number | null;
  notes?: string | null;
  availability?: "available" | "missing" | "unknown";
}

interface SlotItem {
  slot: string;
  group: string;
  label: string;
  hint: string;
  model: string | null;
  current_model?: string | null;
  local_only: boolean; // EFFECTIVE policy (base minus admin cloud opt-in)
  cloud_optionable?: boolean; // confidential slot that can be opened to cloud
  cloud_allowed?: boolean; // admin opted this slot into cloud models
  required_modality?: string | null; // capability the slot needs (backend = source)
  thinking_capable?: boolean; // slot supports a per-assignment reasoning toggle
  thinking_enabled?: boolean | null; // current override (null = model default)
  thinking_supported_by_slot?: boolean;
  thinking_supported_by_model?: boolean;
  thinking_model_default?: boolean | null;
  thinking_override?: boolean | null;
  thinking_effective?: boolean | null;
  thinking_source?: "slot" | "model" | "unsupported";
  thinking_disable_supported?: boolean;
  // За одним переключателем может стоять несколько ролей; если их
  // значения разошлись, показывается состояние первой.
  thinking_mixed?: boolean;
  thinking_warning?: string | null;
  thinking_levels?: string[]; // reasoning-effort levels the SELECTED model supports (empty = none)
  thinking_level_override?: string | null; // this slot's explicit level override
  thinking_level_effective?: string | null; // resolved level actually in effect
}

interface ProvModel extends CatalogEntry {
  thinking_supported: boolean;
  thinking_enabled: boolean;
  thinking_levels?: string[]; // reasoning-effort levels this model accepts (empty = on/off only)
  thinking_level_default?: string | null;
  loaded?: boolean;
  node?: string | null;
  // Pinned node for this model (null = router picks). Rendered as a selector
  // when the provider kind has more than one node — e.g. Ollama GPU vs CPU.
  preferred_instance?: string | null;
}

interface AssignmentIssue {
  slot: string;
  model: string | null;
  code: string;
  message: string;
  severity: "warning" | "error";
}

interface AssignmentDiffItem {
  slot: string;
  old_model: string | null;
  new_model: string | null;
  affected: string[];
}

export function AssignmentBoard() {
  const toast = useToast();
  const [slots, setSlots] = useState<SlotItem[]>([]);
  const [health, setHealth] = useState<Record<string, SlotHealth>>({});
  const [draft, setDraft] = useState<Record<string, string | null>>({});
  // Разрешение облака по слотам — часть черновика, а не отдельное
  // немедленное действие: применяется вместе с моделью.
  const [draftCloud, setDraftCloud] = useState<Record<string, boolean>>({});
  const [models, setModels] = useState<ProvModel[]>([]);
  // Nodes of each local provider kind — a kind can have several (e.g. the GPU
  // Ollama and the CPU-only one), and a model can be pinned to one of them.
  const [nodes, setNodes] = useState<ProviderInstanceT[]>([]);
  const [running, setRunning] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  const [selProv, setSelProv] = useState<string>("");
  const [diff, setDiff] = useState<AssignmentDiffItem[]>([]);
  const [warnings, setWarnings] = useState<AssignmentIssue[]>([]);
  const [errors, setErrors] = useState<AssignmentIssue[]>([]);
  const [lastRevision, setLastRevision] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"validate" | "apply" | "rollback" | null>(
    null,
  );

  const flash = (m: string) => {
    setMsg(m);
    window.setTimeout(() => setMsg(null), 2200);
  };

  const load = useCallback(async () => {
    try {
      const [sl, md, st, nd] = await Promise.all([
        fetch(`${API}/api/providers/assignment-draft`, {
          credentials: "include",
        }),
        fetch(`${API}/api/providers/live-models`, { credentials: "include" }),
        fetch(`${API}/api/local-models/status`, { credentials: "include" }),
        fetch(`${API}/api/providers`, { credentials: "include" }),
      ]);
      if (sl.ok) {
        const d = await sl.json();
        const nextSlots: SlotItem[] = Array.isArray(d?.slots) ? d.slots : [];
        setSlots(nextSlots);
        setDraft(
          Object.fromEntries(nextSlots.map((s) => [s.slot, s.model ?? null])),
        );
        setDiff(d.diff || []);
        setWarnings(d.warnings || []);
        setErrors(d.errors || []);
        setDirty(false);
      }
      if (md.ok) {
        // Каталог обязан быть массивом: объект вместо него ронял весь экран
        // на `models.map` — вместо «моделей не найдено» человек видел белую
        // страницу и не понимал, что сломалось.
        const raw: unknown = await md.json();
        const m: ProvModel[] = Array.isArray(raw) ? (raw as ProvModel[]) : [];
        setModels(m);
        setSelProv((cur) => cur || m[0]?.provider || "");
      }
      // Здоровье слотов — отдельным запросом и молча: это справочная строка,
      // из-за которой экран не должен падать.
      try {
        const hr = await fetch(`${API}/api/providers/slots/health`, {
          credentials: "include",
        });
        if (hr.ok) {
          const list: SlotHealth[] = await hr.json();
          setHealth(Object.fromEntries(list.map((h) => [h.slot, h])));
        }
      } catch {
        /* строка здоровья не обязательна */
      }
      if (st.ok) {
        const s = await st.json();
        const p = s.providers || {};
        setRunning({
          ollama: !!p.ollama?.running,
          llamacpp: !!p.llamacpp?.running,
          vllm: !!p.vllm?.running,
        });
      }
      if (nd.ok) {
        const d = await nd.json();
        setNodes(d.instances || []);
      }
    } catch (e) {
      toast.error("Узлы провайдеров не загрузились", String(e));
      /* ignore */
    }
    setLoading(false);
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const isLocal = (c: ProvModel) => LOCAL_PROVIDERS.includes(c.provider);
  // A physically loaded model is always selectable, even if the catalog marks it
  // disabled (the catalog "disabled" only declutters models that aren't present).
  const selectable = (c: ProvModel) => c.loaded || c.status !== "disabled";

  // Показываем модели ВСЕХ провайдеров, включая облачных: провайдер теперь
  // выбирается явно первым шагом, и этот выбор сам означает решение об облаке.
  // Прежний фильтр по local_only прятал облачные модели у конфиденциальных
  // слотов — и человек не понимал, почему их нет в списке, пока не находил
  // отдельную галочку «разрешить облако» под карточкой.
  const allModelsFor = (_slot: SlotItem): CatalogEntry[] =>
    models.filter(selectable);

  // Решение об облаке едет вместе с моделью в черновике: применится вместе с
  // ней, одним подтверждением, и попадёт в ту же ревизию.
  const setDraftModel = (slot: string, model: string, cloud?: boolean) => {
    setDraft((prev) => ({ ...prev, [slot]: model || null }));
    if (cloud !== undefined) {
      setDraftCloud((prev) => ({ ...prev, [slot]: cloud }));
    }
    setDirty(true);
    setDiff([]);
    setWarnings([]);
    setErrors([]);
  };

  // Черновик в форме, которую ждёт сервер: слот теперь несёт не только модель,
  // но и решение об облаке — раньше оно применялось отдельным немедленным
  // запросом, из-за чего в одной карточке было два разных поведения.
  const draftPayload = () =>
    Object.fromEntries(
      Object.entries(draft).map(([slot, model]) => [
        slot,
        draftCloud[slot] === undefined
          ? { model }
          : { model, allow_cloud: draftCloud[slot] },
      ]),
    );

  const validateDraft = async () => {
    setBusy("validate");
    try {
      const r = await fetch(`${API}/api/providers/assignment-draft/validate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ slots: draftPayload() }),
      });
      if (r.ok) {
        const d = await r.json();
        setSlots(Array.isArray(d?.slots) ? d.slots : []);
        setDiff(d.diff || []);
        setWarnings(d.warnings || []);
        setErrors(d.errors || []);
        flash("Проверка завершена");
      } else {
        const d = await r.json().catch(() => ({}));
        toast.error("Проверка не прошла", String(d.detail || r.status));
      }
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    } finally {
      setBusy(null);
    }
  };

  const applyDraft = async (confirmWarnings = false) => {
    setBusy("apply");
    try {
      const r = await fetch(`${API}/api/providers/assignment-draft/apply`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({
          slots: draftPayload(),
          confirm_warnings: confirmWarnings,
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 409 && d.detail?.warnings && !confirmWarnings) {
        setWarnings(d.detail.warnings);
        if (
          confirm(
            "Есть предупреждения по назначению моделей. Применить всё равно?",
          )
        ) {
          await applyDraft(true);
        }
        return;
      }
      if (!r.ok) {
        toast.error("Назначения не применены", detailText(d));
        return;
      }
      setLastRevision(d.revision_id || null);
      flash("Назначения применены");
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    } finally {
      setBusy(null);
    }
  };

  const rollback = async () => {
    if (!lastRevision) return;
    setBusy("rollback");
    try {
      const r = await fetch(
        `${API}/api/providers/assignments/${lastRevision}/rollback`,
        {
          method: "POST",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        toast.error("Проверка не прошла", String(d.detail || r.status));
        return;
      }
      flash("Последнее изменение откачено");
      setLastRevision(null);
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    } finally {
      setBusy(null);
    }
  };

  const toggleThinking = async (
    key: string,
    enabled: boolean,
    level?: string | null,
  ) => {
    try {
      await fetch(
        `${API}/api/providers/models/${encodeURIComponent(key)}/thinking`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          credentials: "include",
          body: JSON.stringify({ enabled, level: level ?? null }),
        },
      );
      flash(enabled ? "Рассуждение включено" : "Рассуждение выключено");
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  // Enabled nodes of one local provider kind, in listing order.
  const localNodes = (kind: string) =>
    nodes.filter((n) => n.is_local && n.kind === kind && n.enabled);

  // Pin a model to a specific node of its provider kind (e.g. the GPU Ollama
  // vs the CPU-only one). Empty string clears the pin — the router then picks
  // whichever enabled node actually hosts the model.
  const setModelNode = async (key: string, instanceName: string) => {
    try {
      await fetch(
        `${API}/api/providers/models/${encodeURIComponent(key)}/preferred-instance`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            ...(await csrfHeaders()),
          },
          credentials: "include",
          body: JSON.stringify({ instance_name: instanceName || null }),
        },
      );
      flash(
        instanceName ? `Модель закреплена за «${instanceName}»` : "Узел выбирается автоматически",
      );
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  // Reasoning-effort level for a model's default toggle (low/medium/high) —
  // only rendered when the model declares thinking_levels. Reuses the same
  // PATCH endpoint as toggleThinking, keeping enabled=true implicit.
  const setModelThinkingLevel = async (key: string, level: string) => {
    await toggleThinking(key, true, level);
  };

  // Per-assignment reasoning (tri-state): null = model default, true/false force.
  const setSlotThinking = async (
    slot: string,
    enabled: boolean | null,
    level?: string | null,
  ) => {
    try {
      await fetch(`${API}/api/providers/slots/${slot}/thinking`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        credentials: "include",
        body: JSON.stringify({ enabled, level: level ?? null }),
      });
      flash(
        enabled === null
          ? "Рассуждение: по умолчанию"
          : enabled
            ? "Рассуждение включено для слота"
            : "Рассуждение выключено для слота",
      );
      load();
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };

  // Protected setting: opt a confidential slot into cloud models.
  const delModel = async (m: ProvModel) => {
    if (m.provider !== "ollama") {
      toast.error(
        "Удаление поддерживается только для Ollama. Для llama.cpp/vLLM — во вкладке «Библиотека».",
      );
      return;
    }
    if (!confirm(`Удалить модель ${m.provider_model} из Ollama?`)) return;
    try {
      const r = await fetch(
        `${API}/api/local-models/ollama/models/${encodeURIComponent(m.provider_model)}`,
        {
          method: "DELETE",
          headers: await csrfHeaders(),
          credentials: "include",
        },
      );
      if (r.ok) {
        flash("Модель удалена");
        load();
      } else toast.error("Модель не удалена");
    } catch (e) {
      toast.error("Не удалось выполнить действие", String(e));
    }
  };
  const modelByKey = (key: string | null) =>
    key ? models.find((m) => m.key === key) : undefined;
  const providerCanDisableThinking = (provider?: string) =>
    !provider || THINKING_DISABLE_SUPPORTED_PROVIDERS.includes(provider);
  if (loading) return <div className="text-sm text-slate-500">Загрузка…</div>;

  // Порядок групп берём из ответа сервера, а не из литерала: слот новой
  // группы (или переименованной) просто не отрисовывался, без единого следа.
  // Порядок в _SLOTS на бэкенде уже осмысленный, сохранение порядка появления
  // даёт тот же результат и не теряет слоты.
  const groups = Array.from(new Set(slots.map((s) => s.group)));
  const provList = Array.from(new Set(models.map((m) => m.provider)));
  const provModels = models.filter(
    (m) => m.provider === selProv && selectable(m),
  );

  return (
    <div className="space-y-6">
      <RoutingChains />
      <RevisionHistory onRolledBack={load} />
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-400">
          <span className="text-emerald-400">●</span> запущен ·{" "}
          <span className="text-slate-500">○</span> остановлен — vLLM и
          llama.cpp стартуют по требованию. Изменения сначала попадают в
          черновик.
        </p>
        <div className="flex items-center gap-2">
          {msg && <span className="text-xs text-emerald-400">{msg}</span>}
          {lastRevision && (
            <button
              className={`${btnSecondary} text-xs`}
              disabled={busy === "rollback"}
              onClick={rollback}
            >
              Откатить последнее
            </button>
          )}
          <button
            className={`${btnSecondary} text-xs`}
            disabled={!dirty || busy !== null}
            onClick={() => {
              setDraft(
                Object.fromEntries(
                  slots.map((s) => [
                    s.slot,
                    s.current_model ?? s.model ?? null,
                  ]),
                ),
              );
              setDiff([]);
              setWarnings([]);
              setErrors([]);
              setDirty(false);
            }}
          >
            Сбросить
          </button>
          <button
            className={`${btnSecondary} text-xs`}
            disabled={!dirty || busy !== null}
            onClick={validateDraft}
          >
            {busy === "validate" ? "Проверка…" : "Проверить"}
          </button>
          <button
            className={`${btnPrimary} text-xs`}
            disabled={!dirty || busy !== null}
            onClick={() => applyDraft(false)}
          >
            {busy === "apply" ? "Применение…" : "Применить"}
          </button>
        </div>
      </div>

      {(diff.length > 0 || warnings.length > 0 || errors.length > 0) && (
        <div className={card}>
          <div className={cardH}>
            <span className="text-sm font-semibold text-slate-100">
              Проверка черновика
            </span>
            <span className="text-xs text-slate-500">
              {diff.length} изменений · {warnings.length} предупреждений ·{" "}
              {errors.length} ошибок
            </span>
          </div>
          <div className="p-3 space-y-2 text-xs">
            {errors.map((e, i) => (
              <div key={`e-${i}`} className="text-red-300">
                {e.slot}: {e.message}
              </div>
            ))}
            {warnings.map((w, i) => (
              <div key={`w-${i}`} className="text-amber-300">
                {w.slot}: {w.message}
              </div>
            ))}
            {/* Плоская строка «слот: старое → новое» не отвечала на главный
                вопрос — заработает ли это. Панель прогоняет тот же пробный
                вызов, что и проверка после применения, но в режиме dry_run:
                он резолвит каталог, политику, узел и параметры рассуждения,
                ничего не отправляя провайдеру. */}
            <DiffPanel
              diff={diff.map((d) => ({
                ...d,
                label: slots.find((s) => s.slot === d.slot)?.label,
              }))}
              draftFor={(slot) => ({
                model: draft[slot] ?? undefined,
                thinking: slots.find((s) => s.slot === slot)?.thinking_override,
                thinking_level: slots.find((s) => s.slot === slot)
                  ?.thinking_level_override,
              })}
            />
          </div>
        </div>
      )}

      {/* Slots */}
      {groups.map((g) => {
        const gslots = slots.filter((s) => s.group === g);
        if (!gslots.length) return null;
        return (
          <div key={g} className={card}>
            <div className={cardH}>
              <span className="text-sm font-semibold text-slate-100">
                {GROUP_ICON[g]} {g}
              </span>
              {gslots.every((s) => s.local_only) && (
                <span className="text-xs text-slate-500">
                  🔒 конфиденциально — только локальные модели
                </span>
              )}
              {g === "Агент" && (
                <span className="text-xs text-slate-500">
                  можно облачные модели (на ваш выбор)
                </span>
              )}
              {g === "Оцифровка" && (
                <span className="text-xs text-slate-500">
                  🔒 конфиденциально — только локальные модели · метод «по
                  описанию»
                </span>
              )}
            </div>
            <div className="p-4 space-y-3">
              {gslots.map((s) => {
                const chosen = modelByKey(s.current_model ?? s.model);
                const draftValue = draft[s.slot] ?? "";
                const draftChosen = modelByKey(draftValue);
                const slotSupportsThinking =
                  s.thinking_supported_by_slot ?? s.thinking_capable ?? false;
                const selectedSupportsThinking =
                  !!draftChosen?.thinking_supported ||
                  (!draftChosen && !!s.thinking_supported_by_model);
                const thinkingOverride =
                  s.thinking_override ?? s.thinking_enabled ?? null;
                const selectedModelDefault =
                  draftChosen?.thinking_enabled ??
                  s.thinking_model_default ??
                  false;
                const selectedThinkingCapable =
                  slotSupportsThinking && selectedSupportsThinking;
                const effectiveThinking = selectedThinkingCapable
                  ? (thinkingOverride ?? selectedModelDefault)
                  : null;
                const disableSupported = providerCanDisableThinking(
                  draftChosen?.provider,
                );
                const thinkingWarning =
                  selectedThinkingCapable &&
                  effectiveThinking === false &&
                  !disableSupported
                    ? "API этого провайдера может игнорировать выключение reasoning"
                    : s.thinking_warning;
                // Reasoning-effort level (low/medium/high) — only relevant
                // when reasoning is actually ON for the currently selected
                // model and that model declares levels at all.
                const selectedThinkingLevels = draftChosen
                  ? (draftChosen.thinking_levels ?? [])
                  : (s.thinking_levels ?? []);
                const thinkingLevelOverride = s.thinking_level_override ?? null;
                return (
                  <div
                    key={s.slot}
                    className="grid grid-cols-1 sm:grid-cols-[200px_1fr] gap-2 sm:items-start"
                  >
                    <div className="min-w-0">
                      <div className="text-sm text-slate-200">{s.label}</div>
                      <div className="text-xs text-slate-500">{s.hint}</div>
                    </div>
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="flex-1 min-w-0">
                        {/* Два шага: провайдер, затем его модель. Единый
                            список всех моделей выглядел короче, но прятал
                            главное решение — локально или в облако. Оно
                            принималось отдельной галочкой сбоку, которую надо
                            было заметить и связать с выбором. Теперь выбор
                            облачного провайдера и есть это решение. */}
                        <ProviderModelPicker
                          models={allModelsFor(s) as unknown as CatalogModel[]}
                          value={draftValue || null}
                          confidential={Boolean(s.cloud_optionable)}
                          requiredModality={
                            (s.required_modality as Modality | null) ?? null
                          }
                          onChange={(v, opts) =>
                            setDraftModel(s.slot, v, opts.cloud)
                          }
                        />
                      </div>
                      {draftChosen?.key !== chosen?.key && (
                        <span className="text-xs text-amber-400 whitespace-nowrap">
                          черновик
                        </span>
                      )}
                      <SlotThinkingControl
                        state={{
                          supportedBySlot: slotSupportsThinking,
                          supportedByModel: selectedSupportsThinking,
                          modelDefault: selectedModelDefault,
                          override: thinkingOverride,
                          effective: effectiveThinking,
                          disableSupported: s.thinking_disable_supported ?? true,
                          mixed: Boolean(s.thinking_mixed),
                          warning: thinkingWarning ?? null,
                          levels: selectedThinkingLevels as ThinkingLevel[],
                          levelOverride:
                            (thinkingLevelOverride as ThinkingLevel | null) ?? null,
                          levelEffective:
                            (s.thinking_level_effective as ThinkingLevel | null) ??
                            null,
                        }}
                        onChange={(enabled) => setSlotThinking(s.slot, enabled)}
                        onLevelChange={(level) =>
                          setSlotThinking(s.slot, thinkingOverride, level)
                        }
                      />
                    </div>
                    <div className="sm:col-span-2">
                      <SlotHealthStrip health={health[s.slot] ?? null} />
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}

      {/* Per-provider models + thinking toggle */}
      <div className={card}>
        <div className={cardH}>
          <span className="text-sm font-semibold text-slate-100">
            Модели провайдеров
          </span>
          <span className="text-xs text-slate-500">
            все загруженные модели · скачать новые — во вкладке «Библиотека»
          </span>
        </div>
        <div className="flex flex-col sm:flex-row">
          {/* provider list */}
          <div className="sm:w-44 border-b sm:border-b-0 sm:border-r border-slate-700 p-2 space-y-1">
            {provList.map((p) => (
              <button
                key={p}
                onClick={() => setSelProv(p)}
                className={`w-full text-left px-2 py-1.5 rounded text-sm flex items-center gap-2 ${
                  p === selProv
                    ? "bg-blue-600/20 text-blue-200"
                    : "text-slate-300 hover:bg-slate-800"
                }`}
              >
                {p in running && (
                  <span
                    className={
                      running[p] ? "text-emerald-400" : "text-slate-400"
                    }
                  >
                    {running[p] ? "●" : "○"}
                  </span>
                )}
                <span className="flex-1 truncate">{providerLabel(p)}</span>
              </button>
            ))}
          </div>
          {/* models of selected provider */}
          <div className="flex-1 p-2">
            {provModels.length === 0 && (
              <div className="text-xs text-slate-500 px-2 py-3">
                Нет загруженных моделей. Добавьте их во вкладке «Библиотека» или
                подтяните облачные во вкладке «Провайдеры».
              </div>
            )}
            <table className="w-full text-sm">
              <tbody>
                {provModels.map((m) => (
                  <tr key={m.key} className="border-b border-slate-800">
                    <td className="py-1.5 pr-2">
                      <div className="text-slate-200">{m.provider_model}</div>
                      <div className="text-xs text-slate-400">
                        {m.modalities.join(", ")}
                        {m.vram_gb_estimate
                          ? ` · ${m.vram_gb_estimate} GB`
                          : ""}
                        {m.node ? ` · ${m.node}` : ""}
                      </div>
                    </td>
                    <td className="py-1.5 px-2 whitespace-nowrap">
                      <span
                        className={`text-xs px-2 py-0.5 rounded ${
                          m.loaded
                            ? "bg-emerald-700/40 text-emerald-300"
                            : "bg-slate-700/40 text-slate-400"
                        }`}
                      >
                        {m.loaded ? "загружена" : m.status}
                      </span>
                    </td>
                    <td className="py-1.5 px-2 whitespace-nowrap">
                      {localNodes(m.provider).length > 1 && (
                        <select
                          className="rounded border border-slate-600 bg-slate-900 px-1 py-0.5 text-xs text-slate-200"
                          title="На каком узле выполнять эту модель (например GPU или CPU)"
                          value={m.preferred_instance ?? ""}
                          onChange={(e) => setModelNode(m.key, e.target.value)}
                        >
                          <option value="">Узел: авто</option>
                          {localNodes(m.provider).map((n) => (
                            <option key={n.id} value={n.name}>
                              {n.name}
                            </option>
                          ))}
                        </select>
                      )}
                    </td>
                    <td className="py-1.5 px-2 whitespace-nowrap">
                      {m.thinking_supported ? (
                        <div className="flex items-center gap-2">
                          <label className="inline-flex items-center gap-2 text-xs text-slate-300 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={m.thinking_enabled}
                              onChange={(e) =>
                                toggleThinking(m.key, e.target.checked)
                              }
                            />
                            По умолчанию модели
                          </label>
                          {m.thinking_enabled &&
                            (m.thinking_levels?.length ?? 0) > 0 && (
                              <select
                                className="rounded border border-slate-600 bg-slate-900 px-1 py-0.5 text-xs text-slate-200"
                                title="Сила размышления (reasoning effort) по умолчанию для этой модели"
                                value={m.thinking_level_default ?? "medium"}
                                onChange={(e) =>
                                  setModelThinkingLevel(m.key, e.target.value)
                                }
                              >
                                {m.thinking_levels!.map((lvl) => (
                                  <option key={lvl} value={lvl}>
                                    {THINKING_LEVEL_LABEL[lvl] ?? lvl}
                                  </option>
                                ))}
                              </select>
                            )}
                        </div>
                      ) : (
                        <span className="text-xs text-slate-400">без CoT</span>
                      )}
                    </td>
                    <td className="py-1.5 pl-2 text-right whitespace-nowrap">
                      {m.provider === "ollama" && m.loaded && (
                        <button
                          className="text-xs text-red-400 hover:text-red-300"
                          onClick={() => delModel(m)}
                          title="Удалить модель из Ollama"
                        >
                          удалить
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
