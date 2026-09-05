"use client";

import { StatusDot } from "@/components/ui/primitives/StatusDot";
import { formatErrorRate, formatLatency } from "@/lib/models/format";
import { AVAILABILITY_LABEL } from "@/lib/models/labels";

export interface SlotHealth {
  slot: string;
  model: string | null;
  availability: string;
  node: string | null;
  calls: number;
  errors: number;
  error_rate: number;
  avg_latency_ms: number;
  cost_usd: number | null;
  priced: boolean;
}

/**
 * Здоровье слота под карточкой назначения.
 *
 * Все цифры существовали и раньше, но лежали на других вкладках: доступность
 * модели — в каталоге, вызовы и задержка — в телеметрии по моделям. Человек,
 * назначивший модель, не мог узнать, работает ли она, не уходя с экрана.
 */
export function SlotHealthStrip({ health }: { health: SlotHealth | null }) {
  if (!health || !health.model) return null;

  const state =
    health.availability === "available"
      ? ("ok" as const)
      : health.availability === "missing"
        ? ("error" as const)
        : ("unknown" as const);

  // Доля ошибок заметна только на осмысленном числе вызовов: один сбой из
  // двух даёт 50% и ничего не значит.
  const errorTone =
    health.calls >= 20 && health.error_rate >= 0.1
      ? "text-red-400"
      : health.calls >= 20 && health.error_rate > 0
        ? "text-amber-400"
        : "text-slate-400";

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-slate-400">
      <span className="flex items-center gap-1.5">
        <StatusDot
          state={state}
          title={AVAILABILITY_LABEL[health.availability] ?? health.availability}
        />
        {AVAILABILITY_LABEL[health.availability] ?? health.availability}
      </span>

      {health.node && <span>узел {health.node}</span>}

      {health.calls > 0 ? (
        <>
          <span>{health.calls} вызовов</span>
          <span className={errorTone}>
            ошибок {formatErrorRate(health.error_rate)}
          </span>
          {/* Именно средняя: телеметрия хранит сумму и счётчик, медиане
              взяться неоткуда. */}
          <span>средняя {formatLatency(health.avg_latency_ms)}</span>
          {health.priced && health.cost_usd !== null && (
            <span>≈ ${health.cost_usd.toFixed(2)}</span>
          )}
        </>
      ) : (
        <span>вызовов ещё не было</span>
      )}
    </div>
  );
}
