"use client";

import { useMemo } from "react";
import {
  Combobox,
  type ComboboxItem,
} from "@/components/ui/primitives/Combobox";
import { StatusDot } from "@/components/ui/primitives/StatusDot";
import { AVAILABILITY_LABEL, providerLabel } from "@/lib/models/labels";
import type { CatalogModel, Modality } from "@/lib/models/types";
import { ModelFacts } from "./ModelFacts";

/** Почему модель нельзя (или не стоит) назначить на этот слот. */
export interface Ineligibility {
  /** Выбор физически запрещён — не просто предупреждение. */
  blocking: boolean;
  reason: string;
}

const GROUP_AVAILABLE = "Доступно";
const GROUP_NEEDS_ACTION = "Требует действия";
const GROUP_UNSUITABLE = "Не подходит слоту";
const GROUP_ORDER = [GROUP_AVAILABLE, GROUP_NEEDS_ACTION, GROUP_UNSUITABLE];

/**
 * Проверка пригодности модели для слота.
 *
 * Считается по данным каталога и политике слота. Это подсказка для человека,
 * а не решение: допустимость окончательно проверяет бэкенд при валидации
 * черновика — фронтовая копия правил уже расходилась с серверной (списки
 * локальных провайдеров, поддержки отключения рассуждения).
 */
export function checkEligibility(
  model: CatalogModel,
  opts: { requiredModality?: Modality | null; localOnly: boolean },
): Ineligibility | null {
  if (opts.localOnly && !model.local_only) {
    return {
      blocking: true,
      reason:
        "слот работает с содержимым документов — облако нужно разрешить отдельно",
    };
  }
  if (model.availability === "missing") {
    return {
      blocking: false,
      reason: "модели нет ни на одном включённом узле",
    };
  }
  if (
    opts.requiredModality &&
    !model.modalities.includes(opts.requiredModality) &&
    !(opts.requiredModality === "tool_calling" && model.supports_tool_calling)
  ) {
    return {
      blocking: false,
      reason: `модель не заявляет «${opts.requiredModality}»`,
    };
  }
  return null;
}

function availabilityState(model: CatalogModel) {
  if (model.availability === "available") return "ok" as const;
  if (model.availability === "missing") return "error" as const;
  return "unknown" as const;
}

/**
 * Выбор модели для слота: один список с поиском вместо каскада «провайдер →
 * модель». У OpenRouter это сотни опций, и в нативном селекте найти нужную
 * можно было только прокруткой.
 *
 * Непригодные модели не прячутся: человек, который ищет модель и не находит
 * её в списке, не понимает, почему её нет. Они показываются в отдельной
 * группе с объяснением.
 */
export function ModelCombobox({
  models,
  value,
  onChange,
  requiredModality,
  localOnly,
  disabled = false,
}: {
  models: CatalogModel[];
  value: string | null;
  onChange: (modelKey: string) => void;
  requiredModality?: Modality | null;
  localOnly: boolean;
  disabled?: boolean;
}) {
  const items = useMemo<ComboboxItem<CatalogModel>[]>(
    () =>
      models
        .filter((m) => m.status !== "disabled")
        .map((m) => {
          const issue = checkEligibility(m, { requiredModality, localOnly });
          const group = !issue
            ? GROUP_AVAILABLE
            : issue.blocking
              ? GROUP_UNSUITABLE
              : GROUP_NEEDS_ACTION;
          return {
            key: m.key,
            group,
            // Ищем и по имени модели, и по провайдеру: человек помнит либо
            // «qwen», либо «openrouter».
            search: `${m.provider_model} ${m.provider} ${providerLabel(m.provider)} ${m.key}`,
            disabled: issue?.blocking,
            value: m,
          };
        }),
    [models, requiredModality, localOnly],
  );

  const selected = models.find((m) => m.key === value) ?? null;

  return (
    <Combobox
      items={items}
      value={value}
      onChange={onChange}
      disabled={disabled}
      groupOrder={GROUP_ORDER}
      placeholder="Найти модель…"
      emptyText="Ни одна модель не подходит под запрос"
      buttonLabel={
        selected ? (
          <span className="flex items-center gap-2">
            <StatusDot
              state={availabilityState(selected)}
              title={AVAILABILITY_LABEL[selected.availability] ?? ""}
            />
            <span className="truncate">{selected.provider_model}</span>
            <span className="shrink-0 text-xs text-slate-500">
              {providerLabel(selected.provider)}
            </span>
          </span>
        ) : (
          <span className="text-slate-500">Модель не назначена</span>
        )
      }
      renderItem={(item) => {
        const model = item.value;
        const issue = checkEligibility(model, { requiredModality, localOnly });
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
              <span
                className={`text-[11px] ${issue.blocking ? "text-red-400" : "text-amber-400"}`}
              >
                {issue.reason}
              </span>
            )}
          </div>
        );
      }}
    />
  );
}
