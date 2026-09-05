"use client";

import { Badge } from "@/components/ui/primitives/Badge";
import { Segmented } from "@/components/ui/primitives/Segmented";
import { THINKING_LEVEL_LABEL } from "@/lib/models/labels";
import type { ThinkingChoice, ThinkingLevel } from "@/lib/models/types";

export interface ThinkingState {
  /** Слот в принципе допускает рассуждение (у embedding/rerank его нет). */
  supportedBySlot: boolean;
  /** Выбранная модель умеет рассуждать. */
  supportedByModel: boolean;
  /** Значение по умолчанию у модели. */
  modelDefault: boolean | null;
  /** Переопределение слота; null = «как у модели». */
  override: boolean | null;
  /** Что применится в итоге. */
  effective: boolean | null;
  /** Провайдер умеет принудительно выключать рассуждение. */
  disableSupported: boolean;
  /** Поля за одним переключателем разошлись. */
  mixed: boolean;
  warning: string | null;
  levels: ThinkingLevel[];
  levelOverride: ThinkingLevel | null;
  levelEffective: ThinkingLevel | null;
}

const CHOICES: { value: ThinkingChoice; label: string }[] = [
  { value: "auto", label: "Авто" },
  { value: "on", label: "Вкл" },
  { value: "off", label: "Выкл" },
];

function toChoice(override: boolean | null): ThinkingChoice {
  if (override === null) return "auto";
  return override ? "on" : "off";
}

function fromChoice(choice: ThinkingChoice): boolean | null {
  if (choice === "auto") return null;
  return choice === "on";
}

/**
 * Управление рассуждением для одного слота.
 *
 * Раньше это были два контрола с противоположным смыслом: галочка
 * «рассуждение» у модели (прямая логика) и поле `disable_thinking` у роли
 * (обратная). Понять по ним, что применится, было нельзя — тем более что
 * «пусто» в одном означало «как у модели», а в другом «выключено».
 *
 * Здесь одна полярность и три состояния: «Авто» — как у модели, «Вкл» и
 * «Выкл» — решение для этой роли. Что получится в итоге, написано словами.
 */
export function SlotThinkingControl({
  state,
  onChange,
  onLevelChange,
  disabled = false,
}: {
  state: ThinkingState;
  onChange: (enabled: boolean | null) => void;
  onLevelChange: (level: ThinkingLevel | null) => void;
  disabled?: boolean;
}) {
  // Слот, где рассуждения не бывает вовсе (векторизация, переранжирование):
  // показывать выключенный контрол — только сбивать с толку.
  if (!state.supportedBySlot) return null;

  const modelDefaultText =
    state.modelDefault === null
      ? "модель не умеет рассуждать"
      : state.modelDefault
        ? "у модели включено"
        : "у модели выключено";

  const effectiveText = !state.supportedByModel
    ? "модель не умеет рассуждать — настройка ни на что не влияет"
    : state.effective
      ? `применится: рассуждение включено${
          state.levelEffective
            ? `, усилие ${THINKING_LEVEL_LABEL[state.levelEffective]}`
            : ""
        }`
      : "применится: рассуждение выключено";

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <span className="text-xs text-slate-400">Рассуждение</span>
        <Segmented
          label="Рассуждение для этого слота"
          value={toChoice(state.override)}
          options={CHOICES.map((c) => ({
            ...c,
            hint: c.value === "auto" ? modelDefaultText : undefined,
          }))}
          disabled={disabled || !state.supportedByModel}
          onChange={(c) => onChange(fromChoice(c))}
        />
        {state.mixed && (
          <Badge
            tone="warn"
            title="За этим переключателем стоит несколько ролей, и их значения разошлись — показано состояние первой"
          >
            значения разошлись
          </Badge>
        )}
      </div>

      <p className="text-[11px] text-slate-500">{effectiveText}</p>

      {state.supportedByModel && state.effective && state.levels.length > 0 && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-400">Усилие</span>
          <Segmented
            label="Усилие рассуждения"
            value={
              (state.levelOverride ??
                state.levelEffective ??
                "medium") as ThinkingLevel
            }
            options={state.levels.map((l) => ({
              value: l,
              label: THINKING_LEVEL_LABEL[l] ?? l,
            }))}
            disabled={disabled}
            onChange={(l) => onLevelChange(l as ThinkingLevel)}
          />
          {state.levelOverride && (
            <button
              type="button"
              onClick={() => onLevelChange(null)}
              className="text-[11px] text-slate-500 underline hover:text-slate-300"
            >
              как у модели
            </button>
          )}
        </div>
      )}

      {state.warning && (
        <p className="text-[11px] text-amber-400">{state.warning}</p>
      )}
    </div>
  );
}
