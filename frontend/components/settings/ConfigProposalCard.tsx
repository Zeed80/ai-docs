"use client";

import { useState } from "react";

export interface ConfigProposalView {
  id: string;
  setting_path: string;
  proposed_value: unknown;
  current_value: unknown;
  reason: string;
  risk_level: string;
  requested_by: string;
  created_at: string;
  impact?: string | null;
  age_days?: number;
  stale?: boolean;
  current_changed?: boolean;
}

const RISK_LABEL: Record<string, string> = {
  low: "низкий риск",
  medium: "средний риск",
  high: "высокий риск",
  critical: "критично",
};

/**
 * A protected setting waiting for the owner. On 2026-10-21 a two-month-old
 * live-check proposal for system_prompt («medium», two raw values side by
 * side) was approved in a batch and replaced the agent's whole base prompt.
 * The card now says what approving does, flags left-over proposals, and asks
 * twice for high-risk ones.
 */
export function ConfigProposalCard({
  proposal,
  busy,
  format,
  onDecide,
}: {
  proposal: ConfigProposalView;
  busy: boolean;
  format: (value: unknown) => string;
  onDecide: (approved: boolean) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const risky =
    proposal.risk_level === "high" || proposal.risk_level === "critical";
  const critical = proposal.risk_level === "critical";

  return (
    <section
      aria-label={`Предложение ${proposal.setting_path}`}
      className={`rounded border p-3 text-xs ${
        critical
          ? "border-red-800/70 bg-red-950/20"
          : "border-amber-900/60 bg-slate-950/40"
      }`}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-amber-100">
              {proposal.setting_path}
            </span>
            <span
              className={`rounded px-1.5 py-0.5 ${
                critical
                  ? "bg-red-900/60 text-red-100"
                  : "bg-amber-900/50 text-amber-200"
              }`}
            >
              {RISK_LABEL[proposal.risk_level] ?? proposal.risk_level}
            </span>
            <span className="text-slate-400">от {proposal.requested_by}</span>
            {proposal.stale && (
              <span className="rounded bg-slate-700 px-1.5 py-0.5 text-slate-100">
                устарело: ждёт {proposal.age_days} дн.
              </span>
            )}
          </div>
          {proposal.impact && (
            <div className="mt-1 font-medium text-red-200">
              {proposal.impact}
            </div>
          )}
          {proposal.current_changed && (
            <div className="mt-1 text-amber-300">
              Настройка уже изменилась после этого предложения — слева значение
              на момент предложения.
            </div>
          )}
          <div className="mt-1 line-clamp-2 text-slate-400">
            {proposal.reason}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {confirming ? (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  setConfirming(false);
                  onDecide(true);
                }}
                className="rounded bg-red-700 px-2 py-1 text-xs text-white hover:bg-red-600 disabled:opacity-50"
              >
                Да, применить
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => setConfirming(false)}
                className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-100 hover:bg-slate-600 disabled:opacity-50"
              >
                Отмена
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => (risky ? setConfirming(true) : onDecide(true))}
                className="rounded bg-emerald-700 px-2 py-1 text-xs text-white hover:bg-emerald-600 disabled:opacity-50"
              >
                Разрешить
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => onDecide(false)}
                className="rounded bg-slate-700 px-2 py-1 text-xs text-slate-100 hover:bg-slate-600 disabled:opacity-50"
              >
                Отклонить
              </button>
            </>
          )}
        </div>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-2 md:grid-cols-2">
        <div className="min-w-0">
          <div className="mb-1 text-slate-500">Сейчас</div>
          <pre className="max-h-28 overflow-auto whitespace-pre-wrap rounded bg-slate-950/70 p-2 text-slate-400">
            {format(proposal.current_value)}
          </pre>
        </div>
        <div className="min-w-0">
          <div className="mb-1 text-slate-500">Станет</div>
          <pre className="max-h-28 overflow-auto whitespace-pre-wrap rounded bg-slate-950/70 p-2 text-amber-100">
            {format(proposal.proposed_value)}
          </pre>
        </div>
      </div>
    </section>
  );
}
