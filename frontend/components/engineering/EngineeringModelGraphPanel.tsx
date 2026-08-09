"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  engineeringApi,
  EngineeringGraphPatch,
  EngineeringModelGraphRevision,
  EngineeringTraceProposal,
} from "@/lib/engineering-api";

const ORIGIN: Record<string, string> = {
  observed: "Наблюдение",
  traced: "Трассировка",
  derived: "Выведено",
  standard: "Стандарт",
  assumed: "Предположение",
  human: "Инженер",
};

export default function EngineeringModelGraphPanel({
  projectId,
  onError,
}: {
  projectId: string;
  onError: (message: string) => void;
}) {
  const [graphs, setGraphs] = useState<EngineeringModelGraphRevision[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [patches, setPatches] = useState<EngineeringGraphPatch[]>([]);
  const [traces, setTraces] = useState<EngineeringTraceProposal[]>([]);
  const [selectedAssertionId, setSelectedAssertionId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const selected = graphs.find((item) => item.id === selectedId) ?? graphs[0] ?? null;
  const selectedGraphId = selected?.graph_id ?? null;
  const selectedRevisionId = selected?.id ?? null;
  const load = useCallback(async () => {
    try {
      const rows = await engineeringApi.listModelGraphs(projectId);
      setGraphs(rows);
      setSelectedId((current) => current ?? rows[0]?.id ?? null);
    } catch (error) {
      onError(String(error));
    }
  }, [onError, projectId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!selectedGraphId || !selectedRevisionId) {
      setPatches([]);
      setTraces([]);
      return;
    }
    Promise.all([
      engineeringApi.listGraphPatches(selectedGraphId),
      engineeringApi.listTraceProposals(selectedRevisionId),
    ]).then(([patchRows, traceRows]) => {
      setPatches(patchRows);
      setTraces(traceRows);
    }).catch((error) => onError(String(error)));
  }, [onError, selectedGraphId, selectedRevisionId]);

  const assertion = selected?.graph.assertions.find((item) => item.id === selectedAssertionId) ?? null;
  const selectedEvidence = selected?.graph.evidence.filter(
    (item) => assertion?.evidence_ids.includes(item.id),
  ) ?? [];
  const dependentNodes = useMemo(() => {
    if (!selected || !assertion) return [];
    return selected.graph.edges
      .filter((edge) => edge.source_id === assertion.subject_id || edge.target_id === assertion.subject_id)
      .map((edge) => edge.source_id === assertion.subject_id ? edge.target_id : edge.source_id);
  }, [assertion, selected]);

  async function verify() {
    if (!selected) return;
    setBusy(true);
    try {
      await engineeringApi.verifyModelGraph(selected.id);
      await load();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="border border-white/10">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/10 px-4 py-3">
        <div>
          <h2 className="text-sm font-medium text-zinc-100">Engineering Model Graph</h2>
          <p className="mt-1 text-xs text-zinc-500">Канонический граф, evidence, patch и влияние на построение</p>
        </div>
        {selected && (
          <div className="flex items-center gap-3 text-xs">
            <span className="text-zinc-400">r{selected.revision} · {selected.canonical_sha256.slice(0, 12)}</span>
            <button disabled={busy} onClick={() => void verify()} className="text-sky-300 disabled:text-zinc-600">
              Проверить 12 уровней
            </button>
          </div>
        )}
      </div>
      {!selected ? (
        <p className="px-4 py-8 text-sm text-zinc-500">EMG ещё не создан. Legacy CAD/spec продолжают работать как derived views.</p>
      ) : (
        <div className="space-y-4 p-4">
          <div className="flex flex-wrap gap-2 text-xs">
            <select value={selected.id} onChange={(event) => setSelectedId(event.target.value)} className="rounded border border-white/10 bg-zinc-900 px-2 py-1 text-zinc-200">
              {graphs.map((item) => <option key={item.id} value={item.id}>{item.graph_id} · r{item.revision}</option>)}
            </select>
            <Status label="Понимание" value={selected.comprehension_status} />
            <Status label="Построение" value={selected.build_status} />
            <Status label="Выпуск" value={selected.release_status} danger={selected.release_status === "blocked"} />
            <span className="rounded bg-white/5 px-2 py-1 text-zinc-400">
              {selected.graph.nodes.length} узлов · {selected.graph.edges.length} связей
            </span>
          </div>

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(280px,0.7fr)]">
            <div className="max-h-80 overflow-auto border border-white/10">
              <div className="grid grid-cols-[minmax(120px,1fr)_120px_120px] gap-2 border-b border-white/10 px-3 py-2 text-xs text-zinc-500">
                <span>Assertion</span><span>Источник</span><span>Assurance</span>
              </div>
              {selected.graph.assertions.map((item) => (
                <button key={item.id} onClick={() => setSelectedAssertionId(item.id)} className={`grid w-full grid-cols-[minmax(120px,1fr)_120px_120px] gap-2 border-b border-white/5 px-3 py-2 text-left text-xs hover:bg-white/5 ${item.id === selectedAssertionId ? "bg-sky-500/10" : ""}`}>
                  <span className="truncate text-zinc-200">{item.predicate}</span>
                  <span className={item.origin === "assumed" ? "text-amber-300" : item.origin === "traced" ? "text-violet-300" : "text-zinc-400"}>{ORIGIN[item.origin] || item.origin}</span>
                  <span className="truncate text-zinc-400">{item.assurance}</span>
                </button>
              ))}
            </div>
            <div className="border border-white/10 p-3 text-xs text-zinc-400">
              {assertion ? (
                <div className="space-y-2">
                  <p className="font-mono text-zinc-200">{assertion.id}</p>
                  <p>Объект: {assertion.subject_id}</p>
                  <pre className="max-h-24 overflow-auto whitespace-pre-wrap rounded bg-black/20 p-2 text-[11px]">{JSON.stringify(assertion.value, null, 2)}</pre>
                  <p>Влияние: {assertion.impacts.join(", ") || "не заявлено"}</p>
                  <p>Будут пересобраны: {dependentNodes.join(", ") || "нет прямых зависимостей"}</p>
                  <p>Evidence: {assertion.evidence_ids.join(", ") || "нет"}</p>
                  {selectedEvidence.map((item) => (
                    <pre key={item.id} className="max-h-32 overflow-auto whitespace-pre-wrap rounded bg-black/20 p-2 text-[11px]">
                      {JSON.stringify({ id: item.id, kind: item.kind, source_region_id: item.source_region_id, ...item.payload }, null, 2)}
                    </pre>
                  ))}
                </div>
              ) : <p>Выберите assertion, чтобы увидеть значение, bbox/evidence и зависимые узлы.</p>}
            </div>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Log title={`GraphPatch (${patches.length})`} empty="Patch пока не поступали">
              {patches.slice(0, 6).map((item) => (
                <details key={item.id} className="border-b border-white/5 px-3 py-2 text-xs">
                  <summary className={item.accepted ? "text-emerald-300" : "text-red-300"}>{item.patch_id} · {item.producer} · {item.accepted ? "принят" : "отклонён"}</summary>
                  {!!item.validation_errors.length && <p className="mt-2 text-red-300">{item.validation_errors.join(", ")}</p>}
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-[11px] text-zinc-500">{JSON.stringify(item.payload, null, 2)}</pre>
                </details>
              ))}
            </Log>
            <Log title={`Trace proposals (${traces.length})`} empty="Локальная трассировка не запускалась">
              {traces.map((item) => (
                <details key={item.id} className="border-b border-white/5 px-3 py-2 text-xs">
                  <summary className="text-violet-300">#{item.rank} {item.source_region_id} · {item.status} · {item.score ?? "—"}</summary>
                  <p className="mt-2 text-zinc-400">Visual verifier: {item.visual_verifications.length} запуск(ов)</p>
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-[11px] text-zinc-500">{JSON.stringify(item.visual_verifications, null, 2)}</pre>
                </details>
              ))}
            </Log>
          </div>
        </div>
      )}
    </section>
  );
}

function Status({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return <span className={`rounded px-2 py-1 ${danger ? "bg-red-500/10 text-red-300" : "bg-white/5 text-zinc-300"}`}>{label}: {value}</span>;
}

function Log({ title, empty, children }: { title: string; empty: string; children: React.ReactNode }) {
  return <div className="border border-white/10"><h3 className="border-b border-white/10 px-3 py-2 text-xs font-medium text-zinc-300">{title}</h3>{children || <p className="px-3 py-5 text-xs text-zinc-500">{empty}</p>}</div>;
}
