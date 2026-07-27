"use client";

import { useMemo, useState } from "react";

import { correctSpec } from "@/lib/studio-api";
import type { SpecAssumption } from "@/lib/studio-api";

type Section = {
  diameter_mm?: number | null;
  length_mm?: number | null;
  tolerance?: string | null;
  [key: string]: unknown;
};

/** The reading itself, editable, beside the drawing it produced.
 *
 * The person comparing the redraw with the source knows which number is wrong —
 * that is the most valuable information in the system, and until now the only
 * thing they could do with it was redraw the geometry by hand. Correcting the
 * READING instead fixes the part, the sheet and the 3D model at once, because
 * all three are built from it. And the correction is kept as a training pair,
 * so the reader that made the mistake can learn from it.
 *
 * Values the pipeline completed rather than read are marked: those are the ones
 * most worth a second look. */
export default function SpecEditorPanel({
  generationId,
  spec,
  assumptions,
  busy,
  onDone,
  onError,
  t,
}: {
  generationId: string;
  spec: Record<string, unknown> | undefined;
  assumptions?: SpecAssumption[];
  busy: boolean;
  onDone: () => void;
  onError: (message: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const body = (spec?.main_view ?? {}) as Record<string, unknown>;
  const readOuter = useMemo(
    () => ((body.outer ?? []) as Section[]).map((s) => ({ ...s })),
    [body.outer],
  );
  const readBore = useMemo(
    () => ((body.bore ?? []) as Section[]).map((s) => ({ ...s })),
    [body.bore],
  );

  const [outer, setOuter] = useState<Section[]>(readOuter);
  const [bore, setBore] = useState<Section[]>(readBore);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  const assumedFields = useMemo(() => {
    const marked = new Set<string>();
    for (const item of assumptions ?? []) {
      marked.add(`${item.path}.${item.field}`);
    }
    return marked;
  }, [assumptions]);

  if (!spec || (!readOuter.length && !readBore.length)) return null;

  const dirty =
    JSON.stringify(outer) !== JSON.stringify(readOuter) ||
    JSON.stringify(bore) !== JSON.stringify(readBore);

  function update(
    group: "outer" | "bore",
    index: number,
    field: "diameter_mm" | "length_mm",
    raw: string,
  ) {
    const value = raw.trim() === "" ? null : Number(raw.replace(",", "."));
    if (value !== null && !Number.isFinite(value)) return;
    const setter = group === "outer" ? setOuter : setBore;
    const current = group === "outer" ? outer : bore;
    setter(
      current.map((section, position) =>
        position === index ? { ...section, [field]: value } : section,
      ),
    );
  }

  async function save(rebuild: boolean) {
    setSaving(true);
    try {
      await correctSpec(
        generationId,
        { outer, ...(bore.length ? { bore } : {}) },
        { rebuild },
      );
      onDone();
    } catch (error) {
      onError(String((error as Error).message || error));
    } finally {
      setSaving(false);
    }
  }

  const rows = (group: "outer" | "bore", sections: Section[]) =>
    sections.map((section, index) => {
      const path = `main_view.${group}.${index}`;
      return (
        <tr key={`${group}-${index}`} className="border-t border-white/5">
          <td className="py-1 pr-2 text-zinc-500">
            {group === "outer" ? index + 1 : `⌀${index + 1}`}
          </td>
          {(["diameter_mm", "length_mm"] as const).map((field) => {
            const assumed = assumedFields.has(`${path}.${field}`);
            return (
              <td key={field} className="py-1 pr-2">
                <input
                  value={
                    section[field] === null || section[field] === undefined
                      ? ""
                      : String(section[field])
                  }
                  onChange={(e) => update(group, index, field, e.target.value)}
                  disabled={busy || saving}
                  className={`w-24 rounded border bg-zinc-900 px-1.5 py-0.5 text-right text-zinc-200 ${
                    assumed ? "border-amber-500/50" : "border-white/10"
                  }`}
                  title={assumed ? t("vector.spec_editor_assumed") : undefined}
                />
              </td>
            );
          })}
          <td className="py-1 text-[11px] text-zinc-500">
            {String(section.tolerance ?? "")}
          </td>
        </tr>
      );
    });

  return (
    <section className="rounded border border-white/10 bg-zinc-950/60 p-3 text-xs">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center justify-between text-sm font-medium text-zinc-200"
      >
        <span>{t("vector.spec_editor_title")}</span>
        <span className="text-zinc-500">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <>
          <p className="mt-1 text-[11px] text-zinc-500">
            {t("vector.spec_editor_hint")}
          </p>
          <table className="mt-2 w-full">
            <thead className="text-[11px] text-zinc-500">
              <tr>
                <th className="w-8 text-left font-normal">#</th>
                <th className="text-left font-normal">
                  {t("vector.spec_editor_diameter")}
                </th>
                <th className="text-left font-normal">
                  {t("vector.spec_editor_length")}
                </th>
                <th className="text-left font-normal">
                  {t("vector.spec_editor_tolerance")}
                </th>
              </tr>
            </thead>
            <tbody>
              {rows("outer", outer)}
              {rows("bore", bore)}
            </tbody>
          </table>

          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!dirty || busy || saving}
              onClick={() => save(true)}
              className="rounded bg-emerald-600 px-2 py-1 text-white hover:bg-emerald-500 disabled:opacity-40"
            >
              {saving
                ? t("vector.spec_editor_saving")
                : t("vector.spec_editor_rebuild")}
            </button>
            <button
              type="button"
              disabled={!dirty || busy || saving}
              onClick={() => save(false)}
              className="rounded border border-white/15 px-2 py-1 text-zinc-300 hover:bg-white/5 disabled:opacity-40"
            >
              {t("vector.spec_editor_save_only")}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
