"use client";

import { AlertTriangle, ArrowRight } from "lucide-react";
import { useMemo, useState } from "react";

import {
  engineeringApi,
  type EngineeringModelGraphRevision,
} from "@/lib/engineering-api";
import type { EmgOperationNode } from "@/lib/emg-tree";

type Graph = EngineeringModelGraphRevision["graph"];
type Assertion = Graph["assertions"][number];

function valueLabel(assertion: Assertion): string {
  if (assertion.value.kind === "unknown") {
    return String(assertion.value.reason ?? "значение не установлено");
  }
  if ("value" in assertion.value) return String(assertion.value.value);
  return JSON.stringify(assertion.value);
}

/** Global review entry point backed only by sealed EMG relations and options. */
export default function BlockersAlternativesPanel({
  generationId,
  graph,
  operations,
  onNavigate,
}: {
  generationId: string;
  graph: Graph;
  operations: EmgOperationNode[];
  onNavigate: (assertionId: string, operationId: string | null) => void;
}) {
  const [collapsed, setCollapsed] = useState(false);
  const criticalIds = useMemo(
    () => new Set(graph.verification.critical_unresolved_assertion_ids),
    [graph.verification.critical_unresolved_assertion_ids],
  );
  const blockers = useMemo(
    () =>
      graph.assertions
        .filter((item) => item.state === "active" && criticalIds.has(item.id))
        .sort((a, b) => a.id.localeCompare(b.id)),
    [criticalIds, graph.assertions],
  );
  const assertionsById = useMemo(
    () => new Map(graph.assertions.map((item) => [item.id, item])),
    [graph.assertions],
  );
  const optionById = useMemo(
    () => new Map((graph.hypothesis_options ?? []).map((item) => [item.id, item])),
    [graph.hypothesis_options],
  );

  if (!blockers.length) return null;

  const navigate = async (assertion: Assertion, operationId: string | null) => {
    onNavigate(assertion.id, operationId);
    const targetId = graph.build_targets[0]?.id;
    if (operationId || !targetId) return;
    try {
      const impact = await engineeringApi.getGenerationAssertionImpact(
        generationId,
        assertion.id,
        targetId,
      );
      onNavigate(assertion.id, impact.affected_build_operation_ids[0] ?? null);
    } catch {
      // The assertion still opens. Absence of an impact report must not be
      // replaced by a guessed operation link.
    }
  };

  const operationFor = (assertion: Assertion): string | null => {
    const direct = operations.find((item) => item.id === assertion.subject_id);
    if (direct) return direct.id;
    return operations.find((item) => item.featureIds.includes(assertion.subject_id))?.id ?? null;
  };

  return (
    <section className="shrink-0 border-t border-red-500/30 bg-red-500/5" data-testid="cad-blockers-panel">
      <button
        type="button"
        onClick={() => setCollapsed((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[11px] font-medium text-red-300"
      >
        <span>{collapsed ? "▸" : "▾"}</span>
        <AlertTriangle className="h-3.5 w-3.5" />
        Блокеры выпуска: {blockers.length}
      </button>
      {!collapsed && (
        <div className="max-h-64 space-y-2 overflow-y-auto px-3 pb-2 text-[11px]">
          {blockers.map((assertion) => {
            const hypothesisSet = graph.hypothesis_sets.find((set) =>
              set.option_ids.some((optionId) =>
                optionId === assertion.hypothesis_id ||
                Boolean(optionById.get(optionId)?.assertion_ids.includes(assertion.id)),
              ),
            );
            const alternatives = hypothesisSet?.option_ids
              .map((id) => optionById.get(id))
              .filter((item) => item !== undefined) ?? [];
            const operationId = operationFor(assertion);
            return (
              <article key={assertion.id} className="rounded border border-red-500/20 bg-black/20 p-2">
                <button
                  type="button"
                  onClick={() => void navigate(assertion, operationId)}
                  className="flex w-full items-start justify-between gap-2 text-left text-red-100 hover:text-white"
                >
                  <span>
                    <span className="font-medium">{assertion.predicate}</span>
                    <span className="mt-0.5 block text-red-200/70">{valueLabel(assertion)}</span>
                  </span>
                  <ArrowRight className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                </button>
                <p className="mt-1 text-[10px] text-zinc-500">
                  {operationId
                    ? `Переход к ${operationId}`
                    : "Прямая связь не зафиксирована; операция проверяется по server dependency closure"}
                </p>
                {alternatives.length > 1 && (
                  <div className="mt-2 border-t border-white/5 pt-2">
                    <p className="text-[10px] uppercase tracking-wide text-zinc-500">
                      Альтернативы из {hypothesisSet?.id}
                    </p>
                    <div className="mt-1 space-y-1">
                      {alternatives.map((option) => {
                        const facts = option.assertion_ids
                          .map((id) => assertionsById.get(id))
                          .filter((item): item is Assertion => Boolean(item))
                          .map((item) => `${item.predicate}: ${valueLabel(item)}`);
                        return (
                          <div key={option.id} className="rounded border border-white/10 px-2 py-1 text-[10px] text-zinc-300">
                            <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                              <span className="font-mono">{option.id}</span>
                              {hypothesisSet?.selected_option_id === option.id && <span className="text-sky-300">выбрано</span>}
                              <span>evidence {Math.round(option.evidence_coverage * 100)}%</span>
                              <span>views {Math.round(option.cross_view_consistency * 100)}%</span>
                              <span>assumptions {option.assumption_count}</span>
                              {!option.hard_constraints_satisfied && <span className="text-red-300">constraints failed</span>}
                            </div>
                            {facts.length > 0 && <p className="mt-1 break-all text-zinc-500">{facts.join(" · ")}</p>}
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
