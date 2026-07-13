"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useState } from "react";

import { EngineeringProjection, engineeringApi } from "@/lib/engineering-api";

const ENTITY_TYPES: { value: string; label: string }[] = [
  { value: "cad_ir_revision", label: "CAD IR (снимок)" },
  { value: "drawing", label: "Чертёж" },
  { value: "bom", label: "Спецификация (BOM)" },
  { value: "manufacturing_process_plan", label: "Техпроцесс" },
];

const ENTITY_LABEL: Record<string, string> = Object.fromEntries(
  ENTITY_TYPES.map((t) => [t.value, t.label]),
);

// A projection that points at a viewable artifact gets a deep link.
function artifactHref(p: EngineeringProjection): string | null {
  if (p.entity_type === "drawing") return `/drawings/${p.entity_id}`;
  // cad_ir_revision links to the source generation when the metadata carries
  // it (the agent stores generation_id when it creates the projection).
  const genId = (p.metadata?.generation_id as string | undefined) ?? null;
  if (p.entity_type === "cad_ir_revision" && genId) return `/cad/${genId}`;
  return null;
}

export default function ProjectionsPanel({
  revisionId,
  revisionApproved,
  onError,
}: {
  revisionId: string | null;
  revisionApproved: boolean;
  onError: (message: string) => void;
}) {
  const [projections, setProjections] = useState<EngineeringProjection[]>([]);
  const [entityType, setEntityType] = useState("cad_ir_revision");
  const [entityId, setEntityId] = useState("");
  const [projectionType, setProjectionType] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!revisionId) {
      setProjections([]);
      return;
    }
    try {
      setProjections(await engineeringApi.listProjections(revisionId));
    } catch (e) {
      onError(String((e as Error).message || e));
    }
  }, [revisionId, onError]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!revisionId || !entityId.trim()) return;
    setBusy(true);
    try {
      const created = await engineeringApi.createProjection(revisionId, {
        projection_type: projectionType.trim() || entityType,
        entity_type: entityType,
        entity_id: entityId.trim(),
      });
      setProjections((current) => [...current, created]);
      setEntityId("");
      setProjectionType("");
    } catch (e) {
      onError(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="border border-white/10">
      <div className="border-b border-white/10 px-4 py-3">
        <h2 className="text-sm font-medium text-zinc-100">Проекции ревизии</h2>
        <p className="mt-0.5 text-xs text-zinc-500">
          Связанные артефакты: CAD IR, чертёж, спецификация, техпроцесс — с
          признаком актуальности.
        </p>
      </div>

      <div className="divide-y divide-white/5">
        {projections.map((p) => {
          const href = artifactHref(p);
          const stale = p.state === "stale";
          return (
            <div key={p.id} className="px-4 py-3 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-zinc-200">
                  {ENTITY_LABEL[p.entity_type] || p.entity_type}
                  {p.projection_type && p.projection_type !== p.entity_type && (
                    <span className="ml-1 text-xs text-zinc-500">
                      · {p.projection_type}
                    </span>
                  )}
                </span>
                <span
                  className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] ${
                    stale
                      ? "bg-amber-500/15 text-amber-300"
                      : "bg-emerald-500/15 text-emerald-300"
                  }`}
                >
                  {stale ? "устарела" : "актуальна"}
                </span>
              </div>
              <div className="mt-1 flex items-center gap-2 text-xs text-zinc-500">
                <span className="truncate font-mono" title={p.entity_id}>
                  {p.entity_id.slice(0, 8)}
                </span>
                <span>·</span>
                <span>{new Date(p.created_at).toLocaleDateString("ru")}</span>
                {href && (
                  <Link
                    href={href}
                    className="ml-auto text-sky-300 hover:text-sky-100"
                  >
                    Открыть
                  </Link>
                )}
              </div>
            </div>
          );
        })}
        {revisionId && projections.length === 0 && (
          <p className="px-4 py-6 text-sm text-zinc-500">
            Ревизия не связана с артефактами.
          </p>
        )}
        {!revisionId && (
          <p className="px-4 py-6 text-sm text-zinc-500">Выберите ревизию.</p>
        )}
      </div>

      {revisionId && !revisionApproved && (
        <form
          onSubmit={create}
          className="grid gap-2 border-t border-white/10 p-4"
        >
          <select
            value={entityType}
            onChange={(e) => setEntityType(e.target.value)}
            className="rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100"
          >
            {ENTITY_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
          <input
            value={entityId}
            onChange={(e) => setEntityId(e.target.value)}
            placeholder="ID объекта (UUID)"
            className="rounded border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-100"
          />
          <input
            value={projectionType}
            onChange={(e) => setProjectionType(e.target.value)}
            placeholder={`Роль (по умолчанию: ${entityType})`}
            className="rounded border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-100"
          />
          <button
            disabled={busy || !entityId.trim()}
            className="rounded bg-zinc-700 px-3 py-2 text-sm text-zinc-100 hover:bg-zinc-600 disabled:opacity-50"
          >
            Связать артефакт
          </button>
        </form>
      )}
    </section>
  );
}
