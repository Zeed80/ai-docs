"use client";

import { FormEvent, useEffect, useState } from "react";
import Link from "next/link";

import { engineeringApi, EngineeringMaterial, EngineeringProject } from "@/lib/engineering-api";

const STATUS: Record<EngineeringProject["status"], string> = {
  draft: "Черновик", validated: "Проверен", needs_review: "На проверке", approved: "Утвержден", obsolete: "Устарел",
};

const STATUS_COLOR: Record<EngineeringProject["status"], string> = {
  draft: "text-zinc-400", validated: "text-sky-300", needs_review: "text-amber-300", approved: "text-emerald-300", obsolete: "text-zinc-600",
};

export default function EngineeringPage() {
  const [projects, setProjects] = useState<EngineeringProject[]>([]);
  const [materials, setMaterials] = useState<EngineeringMaterial[]>([]);
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      const [projectRows, materialRows] = await Promise.all([engineeringApi.listProjects(), engineeringApi.listMaterials()]);
      setProjects(projectRows);
      setMaterials(materialRows);
    } catch (e) {
      setError(String((e as Error).message || e));
    }
  }

  useEffect(() => { void load(); }, []);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const project = await engineeringApi.createProject({ name: name.trim(), code: code.trim() || undefined });
      setProjects((items) => [project, ...items]);
      setName("");
      setCode("");
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-6 md:px-8">
      <header className="flex flex-wrap items-end justify-between gap-4 border-b border-white/10 pb-4">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-100">Инженерные проекты</h1>
        </div>
        <Link href="/studio" className="rounded bg-sky-600 px-3 py-2 text-sm text-white hover:bg-sky-500">Открыть CAD</Link>
      </header>

      <form onSubmit={create} className="grid gap-3 border-b border-white/10 pb-5 sm:grid-cols-[minmax(0,1fr)_180px_auto]">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Наименование изделия" className="min-w-0 rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100" />
        <input value={code} onChange={(event) => setCode(event.target.value)} placeholder="Обозначение" className="min-w-0 rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100" />
        <button disabled={saving} className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50">Создать</button>
      </form>

      {error && <div className="border-l-2 border-red-400 bg-red-500/5 px-3 py-2 text-sm text-red-300">{error}</div>}

      <section className="overflow-hidden border border-white/10">
        <div className="grid grid-cols-[minmax(0,1fr)_140px_130px] gap-3 border-b border-white/10 bg-zinc-900/70 px-4 py-2 text-xs uppercase text-zinc-500">
          <span>Изделие</span><span>Обозначение</span><span>Состояние</span>
        </div>
        {projects.map((project) => (
          <div key={project.id} className="grid grid-cols-[minmax(0,1fr)_140px_130px] gap-3 border-b border-white/5 px-4 py-3 text-sm last:border-b-0">
            <Link href={`/engineering/${project.id}`} className="truncate text-sky-200 hover:text-sky-100 hover:underline">{project.name}</Link>
            <span className="truncate font-mono text-xs text-zinc-400">{project.code || "-"}</span>
            <span className={STATUS_COLOR[project.status]}>{STATUS[project.status]}</span>
          </div>
        ))}
        {projects.length === 0 && <div className="px-4 py-10 text-center text-sm text-zinc-500">Проекты пока не созданы.</div>}
      </section>

      <section className="border-t border-white/10 pt-4">
        <h2 className="text-sm font-medium text-zinc-200">Материалы</h2>
        <div className="mt-2 flex flex-wrap gap-2">
          {materials.map((material) => <span key={material.id} className="border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-300">{material.designation}{material.standard ? ` · ${material.standard}` : ""}</span>)}
          {materials.length === 0 && <span className="text-xs text-zinc-500">Справочник материалов пуст.</span>}
        </div>
      </section>
    </main>
  );
}
