"use client";

import { focusRing } from "./tokens";

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  /** Подсказка под контролом, когда выбран именно этот вариант. */
  hint?: string;
  disabled?: boolean;
}

/**
 * Сегментированный переключатель на три состояния и меньше.
 *
 * Заведён ради управления рассуждением: раньше оно задавалось галочкой у
 * модели и отдельным полем `disable_thinking` у роли — то есть двумя
 * контролами с противоположной полярностью. Здесь полярность всегда прямая:
 * «Авто · Вкл · Выкл», где «Авто» означает «как у модели».
 */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  disabled = false,
  label,
}: {
  value: T;
  options: SegmentedOption<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  label: string;
}) {
  const active = options.find((o) => o.value === value);

  return (
    <div>
      <div
        role="radiogroup"
        aria-label={label}
        className="inline-flex rounded-md border border-slate-600 bg-slate-900 p-0.5"
      >
        {options.map((opt) => {
          const selected = opt.value === value;
          return (
            <button
              key={opt.value}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={disabled || opt.disabled}
              onClick={() => onChange(opt.value)}
              className={`${focusRing} rounded px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${
                selected
                  ? "bg-blue-600 text-white"
                  : "text-slate-300 hover:bg-slate-800"
              }`}
            >
              {opt.label}
            </button>
          );
        })}
      </div>
      {active?.hint && (
        <p className="mt-1 text-[11px] text-slate-500">{active.hint}</p>
      )}
    </div>
  );
}
