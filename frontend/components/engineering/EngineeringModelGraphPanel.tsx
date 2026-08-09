"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  engineeringApi,
  EngineeringAssertionImpact,
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

const ASSURANCE: Record<string, string> = {
  proposed: "Предложено",
  observed: "Наблюдалось",
  corroborated: "Подтверждено",
  constraint_validated: "Проверено ограничениями",
  human_approved: "Принято инженером",
  contradicted: "Противоречит данным",
};

export default function EngineeringModelGraphPanel({
  projectId,
  generationId,
  onError,
}: {
  projectId?: string;
  generationId?: string;
  onError: (message: string) => void;
}) {
  const [graphs, setGraphs] = useState<EngineeringModelGraphRevision[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [patches, setPatches] = useState<EngineeringGraphPatch[]>([]);
  const [traces, setTraces] = useState<EngineeringTraceProposal[]>([]);
  const [selectedAssertionId, setSelectedAssertionId] = useState<string | null>(null);
  const [targetId, setTargetId] = useState<string>("");
  const [impact, setImpact] = useState<EngineeringAssertionImpact | null>(null);
  const [impactLoading, setImpactLoading] = useState(false);
  const [impactError, setImpactError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [readerTaskId, setReaderTaskId] = useState<string | null>(null);
  const [readerStatus, setReaderStatus] = useState<string | null>(null);
  const [artifactMessage, setArtifactMessage] = useState<string | null>(null);
  const [correctionValue, setCorrectionValue] = useState("");
  const [correctionNote, setCorrectionNote] = useState("");
  const [correctionBbox, setCorrectionBbox] = useState("");
  const [correctionRebuild, setCorrectionRebuild] = useState(false);
  const [correctionMessage, setCorrectionMessage] = useState<string | null>(null);

  const selected = graphs.find((item) => item.id === selectedId) ?? graphs[0] ?? null;
  const selectedGraphId = selected?.graph_id ?? null;
  const selectedRevisionId = selected?.id ?? null;
  const targets = useMemo(() => selected?.graph.build_targets ?? [], [selected]);
  const load = useCallback(async () => {
    try {
      const rows = generationId
        ? [await engineeringApi.getGenerationModelGraph(generationId)]
        : projectId
          ? await engineeringApi.listModelGraphs(projectId)
          : [];
      setGraphs(rows);
      setSelectedId((current) => (
        rows.some((item) => item.id === current) ? current : rows[0]?.id ?? null
      ));
    } catch (error) {
      if (generationId && String(error).startsWith("Error: 404")) {
        setGraphs([]);
        setSelectedId(null);
      } else {
        onError(String(error));
      }
    }
  }, [generationId, onError, projectId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!readerTaskId) return;
    const timer = window.setInterval(() => {
      engineeringApi.getTaskStatus(readerTaskId).then(async (task) => {
        setReaderStatus(task.status);
        if (["SUCCESS", "FAILURE", "REVOKED"].includes(task.status)) {
          window.clearInterval(timer);
          setReaderTaskId(null);
          setBusy(false);
          if (task.status === "SUCCESS") await load();
          else onError(`Reader завершился со статусом ${task.status}`);
        }
      }).catch((error) => {
        window.clearInterval(timer);
        setReaderTaskId(null);
        setBusy(false);
        onError(String(error));
      });
    }, 2500);
    return () => window.clearInterval(timer);
  }, [load, onError, readerTaskId]);
  useEffect(() => {
    if (!selectedGraphId || !selectedRevisionId) {
      setPatches([]);
      setTraces([]);
      return;
    }
    const patchRequest = generationId
      ? engineeringApi.listGenerationGraphPatches(generationId)
      : engineeringApi.listGraphPatches(selectedGraphId);
    const traceRequest = generationId
      ? engineeringApi.listGenerationTraceProposals(generationId)
      : engineeringApi.listTraceProposals(selectedRevisionId);
    Promise.all([patchRequest, traceRequest]).then(([patchRows, traceRows]) => {
      setPatches(patchRows);
      setTraces(traceRows);
    }).catch((error) => onError(String(error)));
  }, [generationId, onError, selectedGraphId, selectedRevisionId]);

  useEffect(() => {
    const preferred = targets.find((item) => item.id === "production") ?? targets[0];
    setTargetId((current) => targets.some((item) => item.id === current) ? current : preferred?.id ?? "");
    setSelectedAssertionId((current) => (
      selected?.graph.assertions.some((item) => item.id === current)
        ? current
        : selected?.graph.assertions.find((item) => item.state === "active")?.id ?? null
    ));
  }, [selected, targets]);

  const assertion = selected?.graph.assertions.find((item) => item.id === selectedAssertionId) ?? null;
  const selectedEvidence = selected?.graph.evidence.filter(
    (item) => assertion?.evidence_ids.includes(item.id),
  ) ?? [];
  const rasterEvidence = selectedEvidence.find((item) => item.kind === "raster_region");
  const assertionTrace = traces.find((item) => item.assertion_id === assertion?.id);
  const nodeById = useMemo(() => new Map(
    selected?.graph.nodes.map((item) => [item.id, item]) ?? [],
  ), [selected]);
  const artifacts = useMemo(() => {
    if (!selected) return [];
    const evidenceById = new Map(selected.graph.evidence.map((item) => [item.id, item]));
    return selected.graph.nodes
      .filter((node) => node.type === "Artifact")
      .flatMap((node) => {
        const assertions = selected.graph.assertions.filter(
          (item) => item.state === "active" && item.subject_id === node.id,
        );
        const evidence = assertions
          .flatMap((item) => item.evidence_ids)
          .map((id) => evidenceById.get(id))
          .find((item) => item?.payload.artifact_path && item.payload.report_path);
        if (!evidence) return [];
        return [{ node, assertions, evidence }];
      });
  }, [selected]);

  useEffect(() => {
    setCorrectionValue(assertion ? JSON.stringify(assertion.value, null, 2) : "");
    setCorrectionNote("");
    setCorrectionRebuild(false);
    const normalized = rasterEvidence?.payload.bbox_normalized;
    setCorrectionBbox(
      Array.isArray(normalized) && normalized.length === 4
        ? normalized.map(String).join(", ")
        : "",
    );
  }, [assertion, rasterEvidence]);
  const artifactBuild = useMemo(() => {
    if (!selected) return null;
    if (selected.profile === "assembly") {
      const assemblyId = selected.graph_id.startsWith("assembly:")
        ? selected.graph_id.slice("assembly:".length)
        : "";
      return assemblyId
        ? { label: "Построить сборочный чертёж", run: () => engineeringApi.buildAssemblyDrawing(assemblyId) }
        : null;
    }
    if (!selected.engineering_revision_id) return null;
    if (selected.profile === "construction") {
      return {
        label: "Построить планы и разрез",
        run: () => engineeringApi.buildConstructionSheets(selected.engineering_revision_id!),
      };
    }
    if (["mep", "electrical", "hydraulic", "pid"].includes(selected.profile)) {
      return {
        label: "Построить схему системы",
        run: () => engineeringApi.buildSystemDiagram(selected.engineering_revision_id!),
      };
    }
    return null;
  }, [selected]);

  useEffect(() => {
    if (!selectedRevisionId || !selectedAssertionId || !targetId) {
      setImpact(null);
      return;
    }
    let active = true;
    setImpactLoading(true);
    setImpactError(null);
    const request = generationId
      ? engineeringApi.getGenerationAssertionImpact(
          generationId, selectedAssertionId, targetId,
        )
      : engineeringApi.getAssertionImpact(
          selectedRevisionId, selectedAssertionId, targetId,
        );
    request
      .then((result) => { if (active) setImpact(result); })
      .catch((error) => {
        if (active) {
          setImpact(null);
          setImpactError(String(error));
        }
      })
      .finally(() => { if (active) setImpactLoading(false); });
    return () => { active = false; };
  }, [generationId, selectedAssertionId, selectedRevisionId, targetId]);

  async function verify() {
    if (!selected) return;
    setBusy(true);
    try {
      if (generationId) await engineeringApi.verifyGenerationModelGraph(generationId);
      else await engineeringApi.verifyModelGraph(selected.id);
      await load();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function submitCorrection() {
    if (!generationId || !assertion) return;
    setBusy(true);
    setCorrectionMessage(null);
    try {
      const value = JSON.parse(correctionValue) as Record<string, unknown>;
      const rawBbox = correctionBbox.trim();
      const bbox = rawBbox
        ? rawBbox.split(",").map((item) => Number(item.trim()))
        : null;
      if (bbox && (bbox.length !== 4 || bbox.some((item) => !Number.isFinite(item)))) {
        throw new Error("bbox должен содержать четыре числа 0..1");
      }
      const revised = await engineeringApi.correctGenerationAssertion(
        generationId,
        assertion.id,
        {
          value,
          note: correctionNote.trim(),
          idempotency_key: `human-ui:${crypto.randomUUID()}`,
          source_bbox_normalized: bbox as [number, number, number, number] | null,
          rebuild: correctionRebuild,
        },
      );
      const replacement = revised.graph.assertions.find(
        (item) => item.supersedes_assertion_id === assertion.id,
      );
      setGraphs([revised]);
      setSelectedId(revised.id);
      setSelectedAssertionId(replacement?.id ?? null);
      if (revised.rebuild_task_id) {
        setReaderStatus("BUILD_QUEUED");
        setReaderTaskId(revised.rebuild_task_id);
      }
      setCorrectionMessage(
        `GraphPatch принят: revision ${revised.revision}`
        + (revised.compatibility_spec_updated ? " · compatibility-view обновлён" : "")
        + (revised.rebuild_task_id ? " · пересборка поставлена в очередь" : ""),
      );
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  }

  async function runReader() {
    if (!selectedGraphId) return;
    setBusy(true);
    setReaderStatus("QUEUED");
    try {
      const task = generationId
        ? await engineeringApi.startGenerationModelReader(
            generationId, targetId || "preview",
          )
        : await engineeringApi.startModelReader(
            selectedGraphId, targetId || "preview",
          );
      setReaderTaskId(task.task_id);
    } catch (error) {
      setBusy(false);
      onError(String(error));
    }
  }

  async function buildDomainArtifact() {
    if (!artifactBuild) return;
    setBusy(true);
    setArtifactMessage(null);
    try {
      const result = await artifactBuild.run();
      setArtifactMessage(
        `${result.idempotent_replay ? "Уже существовал" : "Построен"}: ${result.artifact_sha256.slice(0, 12)} · ${result.production_export_allowed ? "production разрешён" : "provisional"}`,
      );
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
          {selected?.workflow_status === "review_required" && (
            <p className="mt-1 text-xs text-amber-300">
              Чтение и граф сохранены; построение заблокировано до уточнения critical assertions.
            </p>
          )}
        </div>
        {selected && (
          <div className="flex items-center gap-3 text-xs">
            <span className="text-zinc-400">r{selected.revision} · {selected.canonical_sha256.slice(0, 12)}</span>
            <button disabled={busy} onClick={() => void verify()} className="text-sky-300 disabled:text-zinc-600">
              Проверить 12 уровней
            </button>
            <button disabled={busy} onClick={() => void runReader()} className="text-violet-300 disabled:text-zinc-600">
              Адаптивно дочитать
            </button>
            {artifactBuild && (
              <button disabled={busy} onClick={() => void buildDomainArtifact()} className="text-emerald-300 disabled:text-zinc-600">
                {artifactBuild.label}
              </button>
            )}
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
            {!!targets.length && (
              <label className="flex items-center gap-2 rounded border border-white/10 bg-zinc-900 px-2 py-1 text-zinc-400">
                Target
                <select value={targetId} onChange={(event) => setTargetId(event.target.value)} className="bg-transparent text-zinc-200 outline-none">
                  {targets.map((item) => <option key={item.id} value={item.id}>{item.id} · {item.kind}</option>)}
                </select>
              </label>
            )}
            <Status label="Понимание" value={selected.comprehension_status} />
            <Status label="Построение" value={selected.build_status} />
            <Status label="Выпуск" value={selected.release_status} danger={selected.release_status === "blocked"} />
            <span className="rounded bg-white/5 px-2 py-1 text-zinc-400">
              {selected.graph.nodes.length} узлов · {selected.graph.edges.length} связей
            </span>
            <span className="rounded bg-white/5 px-2 py-1 text-zinc-400">
              Reader: {selected.graph.reader_manifest.calls_used}/{selected.graph.reader_manifest.max_model_calls}
              {selected.graph.reader_manifest.stop_reason ? ` · ${selected.graph.reader_manifest.stop_reason}` : ""}
              {readerStatus ? ` · ${readerStatus}` : ""}
            </span>
          </div>
          {artifactMessage && <p className="text-xs text-emerald-300">{artifactMessage}</p>}

          {!generationId && <DomainArtifacts revisionId={selected.id} artifacts={artifacts} />}

          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(280px,0.7fr)]">
            <div className="max-h-80 overflow-auto border border-white/10">
              <div className="grid grid-cols-[minmax(120px,1fr)_120px_120px] gap-2 border-b border-white/10 px-3 py-2 text-xs text-zinc-500">
                <span>Assertion</span><span>Источник</span><span>Assurance</span>
              </div>
              {selected.graph.assertions.map((item) => (
                <button key={item.id} onClick={() => setSelectedAssertionId(item.id)} className={`grid w-full grid-cols-[minmax(120px,1fr)_120px_120px] gap-2 border-b border-white/5 px-3 py-2 text-left text-xs hover:bg-white/5 ${item.id === selectedAssertionId ? "bg-sky-500/10" : ""}`}>
                  <span className="truncate text-zinc-200">{item.predicate}</span>
                  <OriginBadge value={item.origin} />
                  <AssuranceBadge value={item.assurance} />
                </button>
              ))}
            </div>
            <div className="border border-white/10 p-3 text-xs text-zinc-400">
              {assertion ? (
                <div className="space-y-2">
                  <p className="font-mono text-zinc-200">{assertion.id}</p>
                  <p>Объект: {nodeLabel(assertion.subject_id, nodeById)}</p>
                  <div className="flex flex-wrap gap-2"><OriginBadge value={assertion.origin} /><AssuranceBadge value={assertion.assurance} /><span>confidence {Math.round(assertion.confidence * 100)}%</span></div>
                  <pre className="max-h-24 overflow-auto whitespace-pre-wrap rounded bg-black/20 p-2 text-[11px]">{JSON.stringify(assertion.value, null, 2)}</pre>
                  <p>Влияние: {assertion.impacts.join(", ") || "не заявлено"}</p>
                  {impactLoading && <p className="text-sky-300">Рассчитываю dependency impact…</p>}
                  {impactError && <p className="text-red-300">Impact report недоступен: {impactError}</p>}
                  {impact && (
                    <div className="space-y-2 border-l-2 border-white/10 pl-3">
                      <p className={impact.critical_for_target ? "text-red-300" : "text-emerald-300"}>
                        {impact.critical_for_target ? "Критично" : "Некритично"} для target {impact.target_id}
                      </p>
                      <ImpactLine label="Операции" ids={impact.affected_build_operation_ids} nodes={nodeById} />
                      <ImpactLine label="Артефакты" ids={impact.affected_artifact_ids} nodes={nodeById} />
                      <ImpactLine label="Топология" ids={impact.affected_topology_element_ids} nodes={nodeById} />
                      <details>
                        <summary className="cursor-pointer text-zinc-300">Цепочки пересборки ({Object.keys(impact.dependency_paths).length})</summary>
                        <div className="mt-2 max-h-32 space-y-1 overflow-auto font-mono text-[10px] text-zinc-500">
                          {Object.entries(impact.dependency_paths).map(([id, path]) => (
                            <p key={id}>{path.join(" → ")}</p>
                          ))}
                        </div>
                      </details>
                    </div>
                  )}
                  <p>Evidence: {assertion.evidence_ids.join(", ") || "нет"}</p>
                  {selectedEvidence.map((item) => (
                    <pre key={item.id} className="max-h-32 overflow-auto whitespace-pre-wrap rounded bg-black/20 p-2 text-[11px]">
                      {JSON.stringify({ id: item.id, kind: item.kind, source_region_id: item.source_region_id, ...item.payload }, null, 2)}
                    </pre>
                  ))}
                  {generationId && rasterEvidence && (
                    <section className="space-y-2 border-t border-white/10 pt-3">
                      <p className="text-zinc-200">
                        SourceRegion: {rasterEvidence.source_region_id}
                        {rasterEvidence.payload.fallback === true ? " · whole-sheet fallback" : " · точный ROI"}
                      </p>
                      <div className={`grid gap-2 ${assertionTrace ? "sm:grid-cols-2" : ""}`}>
                        <EvidenceImage
                          label="Исходный crop"
                          src={engineeringApi.generationAssertionOverlayUrl(
                            generationId, assertion.id, "source",
                          )}
                        />
                        {assertionTrace && (
                          <>
                            <EvidenceImage
                              label="Candidate"
                              src={engineeringApi.generationAssertionOverlayUrl(
                                generationId, assertion.id, "candidate", assertionTrace.proposal_id,
                              )}
                            />
                            <EvidenceImage
                              label="Overlay"
                              src={engineeringApi.generationAssertionOverlayUrl(
                                generationId, assertion.id, "overlay", assertionTrace.proposal_id,
                              )}
                            />
                            <EvidenceImage
                              label="Difference mask"
                              src={engineeringApi.generationAssertionOverlayUrl(
                                generationId, assertion.id, "difference", assertionTrace.proposal_id,
                              )}
                            />
                          </>
                        )}
                      </div>
                    </section>
                  )}
                  {generationId && assertion.state === "active" && (
                    <section className="space-y-2 border-t border-white/10 pt-3">
                      <p className="font-medium text-zinc-200">Исправить через human GraphPatch</p>
                      <label className="block space-y-1">
                        <span>AssertionValue JSON</span>
                        <textarea
                          aria-label="AssertionValue JSON"
                          value={correctionValue}
                          onChange={(event) => setCorrectionValue(event.target.value)}
                          className="h-28 w-full rounded border border-white/10 bg-black/30 p-2 font-mono text-[11px] text-zinc-200"
                        />
                      </label>
                      <label className="block space-y-1">
                        <span>Source bbox normalized: x0, y0, x1, y1</span>
                        <input
                          aria-label="Source bbox normalized"
                          value={correctionBbox}
                          onChange={(event) => setCorrectionBbox(event.target.value)}
                          placeholder="0.10, 0.20, 0.35, 0.42"
                          className="w-full rounded border border-white/10 bg-black/30 px-2 py-1.5 font-mono text-[11px] text-zinc-200"
                        />
                      </label>
                      <label className="block space-y-1">
                        <span>Инженерное обоснование</span>
                        <textarea
                          aria-label="Инженерное обоснование"
                          value={correctionNote}
                          onChange={(event) => setCorrectionNote(event.target.value)}
                          className="h-20 w-full rounded border border-white/10 bg-black/30 p-2 text-xs text-zinc-200"
                        />
                      </label>
                      {assertion.subject_id === "product:legacy-spec" && (
                        <label className="flex items-center gap-2 text-zinc-300">
                          <input
                            type="checkbox"
                            checked={correctionRebuild}
                            onChange={(event) => setCorrectionRebuild(event.target.checked)}
                          />
                          После GraphPatch пересобрать provisional DXF без повторного VLM-чтения
                        </label>
                      )}
                      <button
                        type="button"
                        disabled={busy || !correctionNote.trim()}
                        onClick={() => void submitCorrection()}
                        className="rounded bg-emerald-500/20 px-3 py-1.5 text-emerald-200 disabled:opacity-40"
                      >
                        Создать human GraphPatch
                      </button>
                      {correctionMessage && <p className="text-emerald-300">{correctionMessage}</p>}
                    </section>
                  )}
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
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-[11px] text-zinc-500">{JSON.stringify(item.payload, null, 2)}</pre>
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

function DomainArtifacts({
  revisionId,
  artifacts,
}: {
  revisionId: string;
  artifacts: Array<{
    node: { id: string; type: string; name?: string | null };
    assertions: EngineeringModelGraphRevision["graph"]["assertions"];
    evidence: EngineeringModelGraphRevision["graph"]["evidence"][number];
  }>;
}) {
  if (!artifacts.length) {
    return (
      <div className="border border-dashed border-white/10 px-3 py-4 text-xs text-zinc-500">
        Проверенных доменных артефактов в этой ревизии пока нет.
      </div>
    );
  }
  return (
    <section className="border border-white/10">
      <h3 className="border-b border-white/10 px-3 py-2 text-xs font-medium text-zinc-300">
        Доменные артефакты ({artifacts.length})
      </h3>
      <div className="grid gap-3 p-3 md:grid-cols-2">
        {artifacts.map(({ node, assertions, evidence }) => {
          const payload = evidence.payload;
          const mediaType = String(payload.media_type || "application/octet-stream");
          const artifactUrl = engineeringApi.graphArtifactUrl(revisionId, node.id);
          const reportUrl = engineeringApi.graphArtifactUrl(revisionId, node.id, "report");
          const views = Array.isArray(payload.views) ? payload.views.map(String) : [];
          const assurance = assertions.map((item) => item.assurance).join(", ");
          return (
            <article key={node.id} className="space-y-2 rounded border border-white/10 bg-black/10 p-3 text-xs text-zinc-400">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-zinc-200">{node.name || node.id}</p>
                  <p className="mt-1 font-mono text-[10px] text-zinc-600">{node.id}</p>
                </div>
                <span className="rounded bg-emerald-500/10 px-2 py-1 text-emerald-300">SHA verified</span>
              </div>
              {mediaType === "image/svg+xml" && (
                <a href={artifactUrl} target="_blank" rel="noreferrer" className="block overflow-hidden rounded bg-white p-2">
                  {/* SVG stays in an isolated image document; it is never injected into the DOM. */}
                  <img src={artifactUrl} alt={node.name || "Engineering artifact"} className="h-40 w-full object-contain" />
                </a>
              )}
              <p>Evidence: {evidence.kind} · {assurance || "—"}</p>
              <p className="break-all font-mono text-[10px]">SHA-256: {String(payload.artifact_sha256 || "—")}</p>
              <p>Виды: {views.join(", ") || "метаданные вида не заданы"}</p>
              <p>
                Покрытие: {payload.required_views_complete === true ? "полное" : "не заявлено"}
                {payload.valid === true ? " · reopen valid" : ""}
              </p>
              <div className="flex gap-3">
                <a href={artifactUrl} target="_blank" rel="noreferrer" className="text-sky-300 hover:text-sky-100">Открыть artifact</a>
                <a href={reportUrl} className="text-violet-300 hover:text-violet-100">Скачать report</a>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function Status({ label, value, danger = false }: { label: string; value: string; danger?: boolean }) {
  return <span className={`rounded px-2 py-1 ${danger ? "bg-red-500/10 text-red-300" : "bg-white/5 text-zinc-300"}`}>{label}: {value}</span>;
}

function OriginBadge({ value }: { value: string }) {
  const style = value === "assumed"
    ? "bg-amber-500/10 text-amber-300"
    : value === "traced"
      ? "bg-violet-500/10 text-violet-300"
      : value === "observed" || value === "human"
        ? "bg-emerald-500/10 text-emerald-300"
        : "bg-white/5 text-zinc-400";
  return <span className={`w-fit rounded px-1.5 py-0.5 ${style}`}>{ORIGIN[value] || value}</span>;
}

function AssuranceBadge({ value }: { value: string }) {
  const style = value === "human_approved" || value === "constraint_validated"
    ? "text-emerald-300"
    : value === "contradicted"
      ? "text-red-300"
      : "text-zinc-400";
  return <span className={`truncate ${style}`}>{ASSURANCE[value] || value}</span>;
}

function nodeLabel(id: string, nodes: Map<string, { id: string; type: string; name?: string | null }>) {
  const node = nodes.get(id);
  return node ? `${node.name || node.id} [${node.type}]` : id;
}

function ImpactLine({ label, ids, nodes }: { label: string; ids: string[]; nodes: Map<string, { id: string; type: string; name?: string | null }> }) {
  return <p>{label}: {ids.map((id) => nodeLabel(id, nodes)).join(", ") || "не затронуты"}</p>;
}

function EvidenceImage({ label, src }: { label: string; src: string }) {
  return (
    <figure className="overflow-hidden rounded border border-white/10 bg-white p-1">
      {/* Raster evidence must stay pixel-exact and authenticated through the API. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={src} alt={label} className="h-36 w-full object-contain" />
      <figcaption className="bg-zinc-950 px-2 py-1 text-[10px] text-zinc-400">{label}</figcaption>
    </figure>
  );
}

function Log({ title, empty, children }: { title: string; empty: string; children: React.ReactNode }) {
  return <div className="border border-white/10"><h3 className="border-b border-white/10 px-3 py-2 text-xs font-medium text-zinc-300">{title}</h3>{children || <p className="px-3 py-5 text-xs text-zinc-500">{empty}</p>}</div>;
}
