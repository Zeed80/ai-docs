"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";

import {
  engineeringApi,
  EngineeringAnalysisCase,
  EngineeringMaterial,
  EngineeringProjectDetail,
  EngineeringRevision,
} from "@/lib/engineering-api";

const STATUS: Record<string, string> = {
  validated: "Проверен",
  needs_review: "На проверке",
  approved: "Утвержден",
  passed: "Пройден",
  failed: "Не пройден",
  draft: "Черновик",
};

export default function EngineeringProjectPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const [project, setProject] = useState<EngineeringProjectDetail | null>(null);
  const [materials, setMaterials] = useState<EngineeringMaterial[]>([]);
  const [selectedRevisionId, setSelectedRevisionId] = useState<string | null>(null);
  const [analysisCases, setAnalysisCases] = useState<EngineeringAnalysisCase[]>([]);
  const [summary, setSummary] = useState("");
  const [caseName, setCaseName] = useState("Осевое напряжение");
  const [force, setForce] = useState("1000");
  const [area, setArea] = useState("100");
  const [materialId, setMaterialId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const selectedRevision = useMemo(
    () => project?.revisions.find((revision) => revision.id === selectedRevisionId) ?? null,
    [project, selectedRevisionId],
  );

  const load = useCallback(async () => {
    try {
      const [detail, materialRows] = await Promise.all([engineeringApi.getProject(projectId), engineeringApi.listMaterials()]);
      setProject(detail);
      setMaterials(materialRows);
      const newest = detail.revisions.at(-1);
      setSelectedRevisionId((current) => current ?? newest?.id ?? null);
    } catch (loadError) {
      setError(String((loadError as Error).message || loadError));
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!selectedRevisionId) {
      setAnalysisCases([]);
      return;
    }
    engineeringApi.listAnalysisCases(selectedRevisionId).then(setAnalysisCases).catch((loadError) => setError(String(loadError)));
  }, [selectedRevisionId]);

  async function createRevision(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const revision = await engineeringApi.createRevision(projectId, {
        base_revision: project?.revisions.at(-1)?.revision ?? null,
        change_summary: summary || undefined,
      });
      setProject((current) => current && { ...current, revisions: [...current.revisions, revision] });
      setSelectedRevisionId(revision.id);
      setSummary("");
    } catch (saveError) {
      setError(String(saveError));
    } finally {
      setBusy(false);
    }
  }

  async function validate() {
    if (!selectedRevision) return;
    setBusy(true);
    setError(null);
    try {
      await engineeringApi.validateRevision(selectedRevision.id);
      await load();
    } catch (saveError) {
      setError(String(saveError));
    } finally {
      setBusy(false);
    }
  }

  async function createCase(event: FormEvent) {
    event.preventDefault();
    if (!selectedRevision) return;
    setBusy(true);
    setError(null);
    try {
      const item = await engineeringApi.createAnalysisCase(selectedRevision.id, {
        name: caseName,
        material_id: materialId || undefined,
        inputs: { force_n: Number(force), area_mm2: Number(area) },
      });
      setAnalysisCases((current) => [...current, item]);
    } catch (saveError) {
      setError(String(saveError));
    } finally {
      setBusy(false);
    }
  }

  async function runCase(caseId: string) {
    setBusy(true);
    setError(null);
    try {
      const next = await engineeringApi.runAnalysisCase(caseId);
      setAnalysisCases((current) => current.map((item) => item.id === next.id ? next : item));
    } catch (saveError) {
      setError(String(saveError));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 md:px-8">
      <header className="flex flex-wrap items-end justify-between gap-3 border-b border-white/10 pb-4">
        <div>
          <Link href="/engineering" className="text-sm text-zinc-400 hover:text-zinc-200">Инженерные проекты</Link>
          <h1 className="mt-1 text-2xl font-semibold text-zinc-100">{project?.name || "Загрузка"}</h1>
          {project?.code && <p className="font-mono text-sm text-zinc-400">{project.code}</p>}
        </div>
        <Link href="/studio" className="rounded bg-sky-600 px-3 py-2 text-sm text-white hover:bg-sky-500">Открыть CAD</Link>
      </header>

      {error && <div className="border-l-2 border-red-400 bg-red-500/5 px-3 py-2 text-sm text-red-300">{error}</div>}

      <section className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(320px,0.8fr)]">
        <div className="border border-white/10">
          <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
            <h2 className="text-sm font-medium text-zinc-100">Ревизии</h2>
            {selectedRevision && <button type="button" disabled={busy || selectedRevision.status === "approved"} onClick={validate} className="text-sm text-sky-300 hover:text-sky-100 disabled:text-zinc-600">Проверить выпуск</button>}
          </div>
          <div className="divide-y divide-white/5">
            {project?.revisions.map((revision: EngineeringRevision) => (
              <button key={revision.id} type="button" onClick={() => setSelectedRevisionId(revision.id)} className={`grid w-full grid-cols-[70px_minmax(0,1fr)_120px] gap-3 px-4 py-3 text-left text-sm hover:bg-white/5 ${revision.id === selectedRevisionId ? "bg-sky-500/10" : ""}`}>
                <span className="font-mono text-zinc-300">R{revision.revision}</span>
                <span className="truncate text-zinc-300">{revision.change_summary || revision.origin}</span>
                <span className="text-right text-xs text-zinc-400">{STATUS[revision.status] || revision.status}</span>
              </button>
            ))}
            {!project?.revisions.length && <p className="px-4 py-8 text-sm text-zinc-500">Создайте первую ревизию изделия.</p>}
          </div>
          <form onSubmit={createRevision} className="flex gap-2 border-t border-white/10 p-3">
            <input value={summary} onChange={(event) => setSummary(event.target.value)} placeholder="Изменение ревизии" className="min-w-0 flex-1 rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100" />
            <button disabled={busy} className="rounded bg-emerald-600 px-3 py-2 text-sm text-white disabled:opacity-50">Новая ревизия</button>
          </form>
        </div>

        <section className="border border-white/10">
          <div className="border-b border-white/10 px-4 py-3"><h2 className="text-sm font-medium text-zinc-100">Расчеты прочности</h2></div>
          <form onSubmit={createCase} className="grid gap-2 p-4">
            <input value={caseName} onChange={(event) => setCaseName(event.target.value)} disabled={!selectedRevision || selectedRevision.status === "approved"} className="rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 disabled:opacity-50" />
            <div className="grid grid-cols-2 gap-2">
              <input value={force} type="number" min="0" onChange={(event) => setForce(event.target.value)} placeholder="Сила, Н" disabled={!selectedRevision || selectedRevision.status === "approved"} className="min-w-0 rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 disabled:opacity-50" />
              <input value={area} type="number" min="0.001" onChange={(event) => setArea(event.target.value)} placeholder="Площадь, мм2" disabled={!selectedRevision || selectedRevision.status === "approved"} className="min-w-0 rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 disabled:opacity-50" />
            </div>
            <select value={materialId} onChange={(event) => setMaterialId(event.target.value)} disabled={!selectedRevision || selectedRevision.status === "approved"} className="rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 disabled:opacity-50">
              <option value="">Без критерия материала</option>
              {materials.map((material) => <option key={material.id} value={material.id}>{material.designation}</option>)}
            </select>
            <button disabled={busy || !selectedRevision || selectedRevision.status === "approved"} className="rounded bg-zinc-700 px-3 py-2 text-sm text-zinc-100 hover:bg-zinc-600 disabled:opacity-50">Добавить расчет</button>
          </form>
          <div className="border-t border-white/10 divide-y divide-white/5">
            {analysisCases.map((item) => <div key={item.id} className="px-4 py-3 text-sm">
              <div className="flex items-center justify-between gap-2"><span className="truncate text-zinc-200">{item.name}</span><span className={item.status === "failed" ? "text-red-300" : "text-zinc-400"}>{STATUS[item.status] || item.status}</span></div>
              {item.results.stress_mpa != null && <p className="mt-1 text-xs text-zinc-400">sigma {item.results.stress_mpa} MPa; запас {item.results.safety_factor ?? "-"}</p>}
              <button type="button" disabled={busy || selectedRevision?.status === "approved"} onClick={() => void runCase(item.id)} className="mt-2 text-xs text-sky-300 hover:text-sky-100 disabled:text-zinc-600">Рассчитать</button>
            </div>)}
            {selectedRevision && analysisCases.length === 0 && <p className="px-4 py-6 text-sm text-zinc-500">Расчеты для ревизии не созданы.</p>}
          </div>
        </section>
      </section>
    </main>
  );
}
