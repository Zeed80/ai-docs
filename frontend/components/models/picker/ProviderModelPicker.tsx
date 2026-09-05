"use client";

import { useMemo } from "react";
import { Badge } from "@/components/ui/primitives/Badge";
import {
  Combobox,
  type ComboboxItem,
} from "@/components/ui/primitives/Combobox";
import { StatusDot } from "@/components/ui/primitives/StatusDot";
import { selectBase } from "@/components/ui/primitives/tokens";
import {
  AVAILABILITY_LABEL,
  isLocalProvider,
  providerLabel,
} from "@/lib/models/labels";
import type {
  CatalogModel,
  ModelCandidate,
  Modality,
} from "@/lib/models/types";
import { ModelFacts } from "./ModelFacts";

const GROUP_AVAILABLE = "Доступно";
const GROUP_NEEDS_ACTION = "Требует внимания";
const GROUP_ORDER = [GROUP_AVAILABLE, GROUP_NEEDS_ACTION];

/**
 * Почему модель не стоит назначать на этот слот.
 *
 * Если сервер прислал вердикт (ModelCandidate), берём его: правила живут в
 * одном месте — там же, где валидация черновика. Своя проверка остаётся
 * запасной для случая, когда список пришёл из каталога без вердикта, и
 * намеренно повторяет только самое очевидное.
 */
function checkModel(
  model: CatalogModel | ModelCandidate,
  requiredModality?: Modality | null,
): string | null {
  const verdict = (model as ModelCandidate).eligibility;
  if (verdict) {
    if (verdict === "ok") return null;
    return (model as ModelCandidate).reasons?.[0]?.message ?? null;
  }
  if (model.availability === "missing") {
    return "модели нет ни на одном включённом узле";
  }
  if (
    requiredModality &&
    !model.modalities.includes(requiredModality) &&
    !(requiredModality === "tool_calling" && model.supports_tool_calling)
  ) {
    return `модель не заявляет «${requiredModality}»`;
  }
  return null;
}

function availabilityState(model: CatalogModel) {
  if (model.availability === "available") return "ok" as const;
  if (model.availability === "missing") return "error" as const;
  return "unknown" as const;
}

/**
 * Выбор модели в два шага: сначала провайдер, потом его модель.
 *
 * Единый список всех моделей сразу выглядел короче, но скрывал главное
 * решение: локальный провайдер или облачный. Оно принималось где-то сбоку —
 * отдельной галочкой «разрешить облачные модели для этого слота», которую
 * ещё надо было заметить и связать с выбором.
 *
 * Теперь решение принимается там же, где выбирается модель: провайдер виден
 * первым, облачные помечены, и выбор облачного и есть разрешение на облако —
 * отдельная галочка не нужна. Список моделей при этом короткий, потому что
 * относится к одному провайдеру, а не ко всем сразу.
 */
export function ProviderModelPicker({
  models,
  value,
  onChange,
  requiredModality,
  /** Слот видит содержимое документов: облачный выбор требует подтверждения. */
  confidential,
  disabled = false,
}: {
  models: CatalogModel[];
  value: string | null;
  onChange: (modelKey: string, opts: { cloud: boolean }) => void;
  requiredModality?: Modality | null;
  confidential: boolean;
  disabled?: boolean;
}) {
  const selectable = useMemo(
    () => models.filter((m) => m.status !== "disabled"),
    [models],
  );

  const selected = selectable.find((m) => m.key === value) ?? null;

  // Провайдеры, у которых есть хоть одна модель. Порядок: локальные первыми —
  // это состояние по умолчанию, облако требует осознанного шага.
  const providers = useMemo(() => {
    const seen = new Map<string, number>();
    for (const m of selectable) {
      seen.set(m.provider, (seen.get(m.provider) ?? 0) + 1);
    }
    return [...seen.entries()]
      .map(([kind, count]) => ({ kind, count, local: isLocalProvider(kind) }))
      .sort((a, b) =>
        a.local === b.local
          ? providerLabel(a.kind).localeCompare(providerLabel(b.kind))
          : a.local
            ? -1
            : 1,
      );
  }, [selectable]);

  // Провайдер берётся из выбранной модели; пока её нет — первый локальный.
  const activeProvider =
    selected?.provider ??
    providers.find((p) => p.local)?.kind ??
    providers[0]?.kind ??
    "";

  const providerModels = useMemo(
    () => selectable.filter((m) => m.provider === activeProvider),
    [selectable, activeProvider],
  );

  const items = useMemo<ComboboxItem<CatalogModel>[]>(
    () =>
      providerModels.map((m) => {
        const issue = checkModel(m, requiredModality);
        return {
          key: m.key,
          group: issue ? GROUP_NEEDS_ACTION : GROUP_AVAILABLE,
          search: `${m.provider_model} ${m.key}`,
          value: m,
        };
      }),
    [providerModels, requiredModality],
  );

  const switchProvider = (kind: string) => {
    // При смене провайдера подставляем его первую пригодную модель: пустой
    // слот после переключения выглядел бы как потеря назначения.
    const candidates = selectable.filter((m) => m.provider === kind);
    const best =
      candidates.find((m) => !checkModel(m, requiredModality)) ?? candidates[0];
    if (best) onChange(best.key, { cloud: !isLocalProvider(kind) });
  };

  const cloudChosen = Boolean(selected && !isLocalProvider(selected.provider));

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <label
          className="sr-only"
          htmlFor={`provider-${requiredModality ?? "any"}`}
        >
          Провайдер
        </label>
        <select
          id={`provider-${requiredModality ?? "any"}`}
          className={`${selectBase} w-44 shrink-0`}
          value={activeProvider}
          disabled={disabled}
          onChange={(e) => switchProvider(e.target.value)}
        >
          <optgroup label="Локальные">
            {providers
              .filter((p) => p.local)
              .map((p) => (
                <option key={p.kind} value={p.kind}>
                  {providerLabel(p.kind)} · {p.count}
                </option>
              ))}
          </optgroup>
          <optgroup label="Облачные">
            {providers
              .filter((p) => !p.local)
              .map((p) => (
                <option key={p.kind} value={p.kind}>
                  {providerLabel(p.kind)} · {p.count}
                </option>
              ))}
          </optgroup>
        </select>

        <div className="min-w-0 flex-1">
          <Combobox
            items={items}
            value={value}
            disabled={disabled}
            groupOrder={GROUP_ORDER}
            placeholder="Найти модель…"
            emptyText="У этого провайдера нет подходящих моделей"
            onChange={(key) =>
              onChange(key, {
                cloud: !isLocalProvider(
                  selectable.find((m) => m.key === key)?.provider ?? "",
                ),
              })
            }
            buttonLabel={
              selected ? (
                <span className="flex items-center gap-2">
                  <StatusDot
                    state={availabilityState(selected)}
                    title={AVAILABILITY_LABEL[selected.availability] ?? ""}
                  />
                  <span className="truncate">{selected.provider_model}</span>
                </span>
              ) : (
                <span className="text-slate-500">Модель не назначена</span>
              )
            }
            renderItem={(item) => {
              const model = item.value;
              const issue = checkModel(model, requiredModality);
              return (
                <div className="flex flex-col gap-0.5">
                  <span className="flex items-center gap-2">
                    <StatusDot
                      state={availabilityState(model)}
                      title={AVAILABILITY_LABEL[model.availability] ?? ""}
                    />
                    <span className="truncate text-sm text-slate-200">
                      {model.provider_model}
                    </span>
                  </span>
                  <ModelFacts model={model} />
                  {issue && (
                    <span className="text-[11px] text-amber-400">{issue}</span>
                  )}
                </div>
              );
            }}
          />
        </div>

        {cloudChosen && (
          <Badge
            tone="warn"
            title="Содержимое запросов уйдёт внешнему провайдеру"
          >
            облако
          </Badge>
        )}
      </div>

      {cloudChosen && confidential && (
        // Отдельной галочки «разрешить облако» больше нет: выбор облачного
        // провайдера и есть это решение. Но слот работает с содержимым
        // документов, поэтому решение должно быть названо вслух — молча
        // отправлять такие данные наружу нельзя.
        <p className="text-[11px] text-amber-400">
          Через этот слот проходит содержимое документов. Выбрав облачного
          провайдера, вы разрешаете отправлять их наружу — это применится вместе
          с назначением.
        </p>
      )}
    </div>
  );
}
