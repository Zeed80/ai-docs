"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import {
  Workflow,
  deleteWorkflow,
  duplicateWorkflow,
  listWorkflows,
  patchWorkflow,
} from "@/lib/studio-api";

export default function WorkflowPanel() {
  const t = useTranslations("studio.workflow");
  const [items, setItems] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<Workflow | null>(null);

  async function load() {
    setLoading(true);
    setErr(null);
    try {
      setItems(await listWorkflows());
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const byCategory = items.reduce<Record<string, Workflow[]>>((acc, w) => {
    (acc[w.category] ??= []).push(w);
    return acc;
  }, {});

  if (loading)
    return <div className="text-sm text-zinc-500">{t("loading")}</div>;
  if (err) return <div className="text-sm text-red-400">{err}</div>;

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <div className="space-y-4">
        {Object.entries(byCategory).map(([cat, ws]) => (
          <div key={cat}>
            <h4 className="text-xs uppercase tracking-wide text-zinc-500 mb-2">
              {t.has(`category.${cat}`) ? t(`category.${cat}`) : cat}
            </h4>
            <div className="space-y-1">
              {ws.map((w) => (
                <button
                  key={w.id}
                  onClick={() => setSelected(w)}
                  className={`w-full text-left px-3 py-2 rounded border text-sm ${
                    selected?.id === w.id
                      ? "border-sky-500 bg-sky-500/10"
                      : "border-white/10 hover:border-white/30"
                  } ${w.enabled ? "" : "opacity-50"}`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-zinc-200">{w.title}</span>
                    {w.is_builtin && (
                      <span className="text-[10px] text-zinc-500">
                        {t("builtin")}
                      </span>
                    )}
                  </div>
                  {w.description && (
                    <div className="text-[11px] text-zinc-500 line-clamp-2">
                      {w.description}
                    </div>
                  )}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="rounded-lg border border-white/10 p-3 bg-zinc-900/40">
        {!selected ? (
          <div className="text-sm text-zinc-500">{t("select_hint")}</div>
        ) : (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-medium text-zinc-200">
                {selected.title}
              </h3>
              <span className="text-[11px] text-zinc-500">
                {selected.operation}
              </span>
            </div>
            {selected.description && (
              <p className="text-xs text-zinc-400">{selected.description}</p>
            )}
            <div className="flex flex-wrap gap-2">
              <button
                onClick={async () => {
                  await duplicateWorkflow(selected.id);
                  await load();
                }}
                className="px-2.5 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
              >
                {t("duplicate")}
              </button>
              <button
                onClick={async () => {
                  const upd = await patchWorkflow(selected.id, {
                    enabled: !selected.enabled,
                  });
                  setSelected(upd);
                  await load();
                }}
                disabled={selected.is_builtin && false}
                className="px-2.5 py-1 rounded bg-white/10 hover:bg-white/20 text-xs"
              >
                {selected.enabled ? t("disable") : t("enable")}
              </button>
              {!selected.is_builtin && (
                <button
                  onClick={async () => {
                    await deleteWorkflow(selected.id);
                    setSelected(null);
                    await load();
                  }}
                  className="px-2.5 py-1 rounded bg-red-500/15 hover:bg-red-500/25 text-red-300 text-xs"
                >
                  {t("delete")}
                </button>
              )}
            </div>
            {selected.is_builtin && (
              <p className="text-[11px] text-amber-400/80">
                {t("builtin_readonly")}
              </p>
            )}
            <details>
              <summary className="text-xs text-zinc-400 cursor-pointer">
                {t("graph_json")}
              </summary>
              <pre className="mt-2 max-h-72 overflow-auto text-[11px] text-zinc-400 bg-black/40 rounded p-2">
                {JSON.stringify(selected.graph, null, 2)}
              </pre>
            </details>
          </div>
        )}
      </div>
    </div>
  );
}
