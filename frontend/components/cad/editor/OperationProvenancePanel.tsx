"use client";

import { ExternalLink } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import {
  engineeringApi,
  type EngineeringAssertionImpact,
  type EngineeringModelGraphRevision,
} from "@/lib/engineering-api";
import type { EmgOperationNode } from "@/lib/emg-tree";

type Graph = EngineeringModelGraphRevision["graph"];
type Assertion = Graph["assertions"][number];
type Evidence = Graph["evidence"][number];

type ProvenanceClass = "observed" | "inferred" | "human" | "unresolved";

const CLASS_LABEL: Record<ProvenanceClass, string> = {
  observed: "Наблюдено",
  inferred: "Выведено",
  human: "Исправлено человеком",
  unresolved: "Не разрешено",
};

const CLASS_TONE: Record<ProvenanceClass, string> = {
  observed: "bg-sky-500/15 text-sky-300",
  inferred: "bg-amber-500/15 text-amber-300",
  human: "bg-emerald-500/15 text-emerald-300",
  unresolved: "bg-red-500/15 text-red-300",
};

function provenanceClass(assertion: Assertion): ProvenanceClass {
  if (assertion.value.kind === "unknown" || assertion.state !== "active") {
    return "unresolved";
  }
  if (assertion.origin === "human") return "human";
  if (assertion.origin === "observed" || assertion.origin === "traced") {
    return "observed";
  }
  return "inferred";
}

function displayValue(value: Record<string, unknown>): string {
  if (value.kind === "unknown") {
    return String(value.reason ?? "значение отсутствует");
  }
  if ("value" in value) return String(value.value);
  return JSON.stringify(value);
}

function bboxLabel(evidence: Evidence): string | null {
  const bbox = evidence.payload.bbox_normalized;
  if (!Array.isArray(bbox) || bbox.length !== 4) return null;
  const values = bbox.map((item) => Number(item));
  if (values.some((item) => !Number.isFinite(item))) return null;
  return values.map((item) => item.toFixed(3)).join(", ");
}

function pageLabel(evidence: Evidence): string {
  const raw = evidence.payload.page_index ?? evidence.payload.image_index;
  const index = Number(raw);
  return Number.isInteger(index) && index >= 0 ? `лист ${index + 1}` : "лист 1";
}

function NodeList({
  label,
  ids,
  nodeNames,
  dependencyPaths,
}: {
  label: string;
  ids: string[];
  nodeNames: Map<string, string>;
  dependencyPaths?: Record<string, string[]>;
}) {
  if (!ids.length) return null;
  return (
    <div>
      <p className="text-zinc-500">{label}</p>
      <div className="mt-1 space-y-1 font-mono text-[10px] text-zinc-300">
        {ids.map((id) => (
          <div key={id}>
            <p className="break-all">{nodeNames.get(id) ?? id}</p>
            {dependencyPaths?.[id] && (
              <p className="break-all text-[9px] text-zinc-400">
                ↳ {dependencyPaths[id].join(" → ")}
              </p>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

/** Read-only evidence inspector for the selected immutable design operation.
 * It deliberately presents graph facts and the server-calculated dependency
 * closure; it never promotes inference to observed geometry in the client. */
export default function OperationProvenancePanel({
  generationId,
  graph,
  operation,
  focusedAssertionId,
}: {
  generationId: string;
  graph: Graph;
  operation: EmgOperationNode | null;
  focusedAssertionId?: string | null;
}) {
  const assertions = useMemo(() => {
    const subjects = new Set(
      operation ? [operation.id, ...operation.featureIds] : [],
    );
    const scoped = graph.assertions
      .filter(
        (item) => item.state === "active" && subjects.has(item.subject_id),
      )
      .sort((a, b) => a.predicate.localeCompare(b.predicate));
    const focused = graph.assertions.find(
      (item) => item.state === "active" && item.id === focusedAssertionId,
    );
    return focused && !scoped.some((item) => item.id === focused.id)
      ? [focused, ...scoped]
      : scoped;
  }, [focusedAssertionId, graph.assertions, operation]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [impact, setImpact] = useState<EngineeringAssertionImpact | null>(null);
  const [impactError, setImpactError] = useState<string | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);

  useEffect(() => {
    setSelectedId((current) =>
      focusedAssertionId && assertions.some((item) => item.id === focusedAssertionId)
        ? focusedAssertionId
        : current && assertions.some((item) => item.id === current)
          ? current
          : (assertions[0]?.id ?? null),
    );
  }, [assertions, focusedAssertionId]);

  const assertion =
    assertions.find((item) => item.id === selectedId) ?? assertions[0] ?? null;
  const evidenceById = useMemo(
    () => new Map(graph.evidence.map((item) => [item.id, item])),
    [graph.evidence],
  );
  const evidence = assertion
    ? assertion.evidence_ids
        .map((id) => evidenceById.get(id))
        .filter((item): item is Evidence => Boolean(item))
    : [];
  const rasterEvidence = evidence.find((item) => bboxLabel(item));
  const targetId = graph.build_targets[0]?.id ?? null;
  const nodeNames = useMemo(
    () =>
      new Map(
        graph.nodes.map((node) => [
          node.id,
          node.name ? `${node.name} (${node.id})` : node.id,
        ]),
      ),
    [graph.nodes],
  );

  useEffect(() => {
    let cancelled = false;
    setImpact(null);
    setImpactError(null);
    if (!assertion || !targetId) return () => undefined;
    setImpactLoading(true);
    void engineeringApi
      .getGenerationAssertionImpact(generationId, assertion.id, targetId)
      .then((result) => {
        if (!cancelled) setImpact(result);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setImpactError(error instanceof Error ? error.message : String(error));
        }
      })
      .finally(() => {
        if (!cancelled) setImpactLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [assertion, generationId, targetId]);

  if (!operation && !focusedAssertionId) return null;

  return (
    <section
      className="border-b border-white/10"
      data-testid="operation-provenance-panel"
    >
      <div className="px-3 py-2">
        <h3 className="text-xs font-medium text-zinc-200">Источник и влияние</h3>
        <p className="mt-1 text-[10px] text-zinc-500">
          Операция → утверждение → bbox → зависимые узлы
        </p>
      </div>
      {!assertions.length ? (
        <p className="px-3 pb-3 text-[11px] text-zinc-500">
          Для операции нет активных утверждений с provenance.
        </p>
      ) : (
        <div className="space-y-3 px-3 pb-3">
          <div className="flex gap-1 overflow-x-auto pb-1">
            {assertions.map((item) => {
              const category = provenanceClass(item);
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setSelectedId(item.id)}
                  className={`shrink-0 rounded border px-2 py-1 text-[10px] ${
                    item.id === assertion?.id
                      ? "border-sky-400/60 bg-sky-500/10 text-sky-200"
                      : "border-white/10 text-zinc-400 hover:bg-white/5"
                  }`}
                  title={`${CLASS_LABEL[category]} · ${item.predicate}`}
                >
                  {item.predicate.replace(/^(operation|feature)\./, "")}
                </button>
              );
            })}
          </div>

          {assertion && (
            <div className="grid gap-2 sm:grid-cols-2">
              <div className="min-w-0 rounded border border-white/10 bg-black/15 p-2 text-[11px]">
                {rasterEvidence ? (
                  <>
                    {/* Authenticated, pixel-exact crop rendered by the backend. */}
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={engineeringApi.generationAssertionOverlayUrl(
                        generationId,
                        assertion.id,
                        "source",
                      )}
                      alt={`Источник ${assertion.predicate}`}
                      className="h-28 w-full rounded bg-white object-contain"
                    />
                    <div className="mt-2 flex items-start justify-between gap-2">
                      <p className="break-all text-zinc-400">
                        {pageLabel(rasterEvidence)} · bbox {bboxLabel(rasterEvidence)}
                        {rasterEvidence.payload.fallback === true
                          ? " · весь лист"
                          : " · точный ROI"}
                      </p>
                      <a
                        href={engineeringApi.generationAssertionOverlayUrl(
                          generationId,
                          assertion.id,
                          "overlay",
                        )}
                        target="_blank"
                        rel="noreferrer"
                        className="shrink-0 text-sky-300 hover:text-sky-200"
                        aria-label="Открыть bbox на полном листе"
                      >
                        <ExternalLink className="h-3.5 w-3.5" />
                      </a>
                    </div>
                  </>
                ) : (
                  <div className="flex h-28 items-center justify-center rounded border border-dashed border-white/10 text-center text-zinc-500">
                    Точный bbox не приложен
                  </div>
                )}
              </div>

              <div className="min-w-0 space-y-2 rounded border border-white/10 bg-black/15 p-2 text-[11px]">
                <div className="flex flex-wrap items-center gap-1">
                  <span
                    className={`rounded px-1.5 py-0.5 ${CLASS_TONE[provenanceClass(assertion)]}`}
                  >
                    {CLASS_LABEL[provenanceClass(assertion)]}
                  </span>
                  <span className="text-zinc-500">
                    {Math.round(assertion.confidence * 100)}%
                  </span>
                </div>
                <p className="break-all font-mono text-[10px] text-zinc-500">
                  {assertion.subject_id}
                </p>
                <p className="text-zinc-200">
                  {assertion.predicate}: {displayValue(assertion.value)}
                </p>
                <p className="text-zinc-500">
                  assurance: {assertion.assurance}
                </p>
                <p className="text-zinc-400">
                  Влияние: {assertion.impacts.join(", ") || "не заявлено"}
                </p>
              </div>
            </div>
          )}

          <div className="rounded border border-white/10 bg-black/15 p-2 text-[11px]">
            {impactLoading && (
              <p className="text-sky-300">Проверяю зависимости…</p>
            )}
            {impactError && (
              <p className="text-red-300">Impact report недоступен: {impactError}</p>
            )}
            {impact && (
              <div className="space-y-2">
                <p
                  className={
                    impact.critical_for_target
                      ? "text-red-300"
                      : "text-emerald-300"
                  }
                >
                  {impact.critical_for_target ? "Критично" : "Некритично"} для
                  сборки {impact.target_id}
                </p>
                <NodeList
                  label="Операции"
                  ids={impact.affected_build_operation_ids}
                  nodeNames={nodeNames}
                />
                <NodeList
                  label="Топология"
                  ids={impact.affected_topology_element_ids}
                  nodeNames={nodeNames}
                />
                <NodeList
                  label="Артефакты"
                  ids={impact.affected_artifact_ids}
                  nodeNames={nodeNames}
                />
                <div className="border-t border-white/5 pt-2">
                  {(impact.affected_view_ids ?? []).length ? (
                    <NodeList
                      label="Связанные виды"
                      ids={impact.affected_view_ids ?? []}
                      nodeNames={nodeNames}
                      dependencyPaths={impact.dependency_paths}
                    />
                  ) : (
                    <p className="text-zinc-500">
                      Межвидовая связь для этого утверждения не зафиксирована.
                    </p>
                  )}
                </div>
                <div className="border-t border-white/5 pt-2">
                  {(impact.affected_bim_object_ids ?? []).length ? (
                    <NodeList
                      label="Связанные BIM/IFC объекты"
                      ids={impact.affected_bim_object_ids ?? []}
                      nodeNames={nodeNames}
                      dependencyPaths={impact.dependency_paths}
                    />
                  ) : (
                    <p className="text-zinc-500">
                      BIM/IFC связь для этого утверждения не зафиксирована.
                    </p>
                  )}
                </div>
                {!impact.affected_build_operation_ids.length &&
                  !impact.affected_topology_element_ids.length &&
                  !impact.affected_artifact_ids.length && (
                    <p className="text-zinc-500">Зависимые узлы не найдены.</p>
                  )}
              </div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
