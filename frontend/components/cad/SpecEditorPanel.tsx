"use client";

import { useEffect, useMemo, useState } from "react";

import { correctSpec } from "@/lib/studio-api";
import type { SpecAssumption } from "@/lib/studio-api";

type Section = {
  diameter_mm?: number | null;
  length_mm?: number | null;
  tolerance?: string | null;
  [key: string]: unknown;
};

type AxialHolePattern = {
  count?: number | null;
  bolt_circle_diameter_mm?: number | null;
  start_angle_deg?: number | null;
  spacing_deg?: number | null;
  from_face?: "zmin" | "zmax" | null;
  entry_offset_mm?: number | null;
  entry_recess_diameter_mm?: number | null;
  through?: boolean | null;
  depth_mm?: number | null;
  thread_depth_mm?: number | null;
  drill_depth_mm?: number | null;
  pilot_diameter_mm?: number | null;
  thread?: {
    designation?: string;
    nominal_diameter_mm?: number | null;
    pitch_mm?: number | null;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

type Chamfer = {
  size_mm?: number | null;
  angle_deg?: number | null;
  location?: "left_end" | "right_end" | "shoulder" | "bore_mouth";
  at_z_mm?: number | null;
  at_diameter_mm?: number | null;
  [key: string]: unknown;
};

type CircularHolePattern = {
  count?: number | null;
  hole_diameter_mm?: number | null;
  bolt_circle_diameter_mm?: number | null;
  axis_mode?: "axial" | "inclined";
  start_angle_deg?: number | null;
  spacing_deg?: number | null;
  from_face?: "zmin" | "zmax" | null;
  entry_offset_mm?: number | null;
  through?: boolean | null;
  depth_mm?: number | null;
  inclination_deg?: number | null;
  radial_direction?: "outward" | "inward" | null;
  connection_station_mm?: number | null;
  [key: string]: unknown;
};

function numberFromInput(raw: string): number | null | undefined {
  if (raw.trim() === "") return null;
  const value = Number(raw.replace(",", "."));
  return Number.isFinite(value) ? value : undefined;
}

function shown(value: number | null | undefined): string {
  return value === null || value === undefined ? "" : String(value);
}

function expectedChamferCount(spec: Record<string, unknown>): number {
  const sources: Record<string, unknown>[] = [
    ...((spec.dimensions ?? []) as Record<string, unknown>[]),
    ...((spec.annotations ?? []) as Record<string, unknown>[]),
    ...((spec.unresolved ?? []) as unknown[]).map(
      (value): Record<string, unknown> => ({ value }),
    ),
  ];
  let expected = 0;
  for (const item of sources) {
    const text = [item?.value, item?.text, item?.raw_text, item?.label]
      .filter((value) => value !== null && value !== undefined)
      .join(" ");
    const match = text.match(/\b(\d+)\s*(?:фас(?:ок|ки|ка)?|chamfers?)/iu);
    if (match) expected = Math.max(expected, Number(match[1]));
  }
  return expected;
}

/** Human correction of the exact data handed from the reader to the CAD kernel.
 * Unknown source values remain empty: hints are shown separately and are never
 * copied into the spec without an explicit user edit. */
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
    () => ((body.outer ?? []) as Section[]).map((item) => ({ ...item })),
    [body.outer],
  );
  const readBore = useMemo(
    () => ((body.bore ?? []) as Section[]).map((item) => ({ ...item })),
    [body.bore],
  );
  const readAxialHoles = useMemo(
    () =>
      ((body.axial_holes ?? []) as AxialHolePattern[]).map((item) => ({
        ...item,
        thread: item.thread ? { ...item.thread } : undefined,
      })),
    [body.axial_holes],
  );
  const readChamfers = useMemo(
    () => ((body.chamfers ?? []) as Chamfer[]).map((item) => ({ ...item })),
    [body.chamfers],
  );
  const readCircularPatterns = useMemo(
    () =>
      ((body.circular_hole_patterns ?? []) as CircularHolePattern[]).map(
        (item) => ({ ...item }),
      ),
    [body.circular_hole_patterns],
  );

  const [outer, setOuter] = useState<Section[]>(readOuter);
  const [bore, setBore] = useState<Section[]>(readBore);
  const [axialHoles, setAxialHoles] =
    useState<AxialHolePattern[]>(readAxialHoles);
  const [chamfers, setChamfers] = useState<Chamfer[]>(readChamfers);
  const [circularPatterns, setCircularPatterns] =
    useState<CircularHolePattern[]>(readCircularPatterns);
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(true);

  useEffect(() => setOuter(readOuter), [readOuter]);
  useEffect(() => setBore(readBore), [readBore]);
  useEffect(() => setAxialHoles(readAxialHoles), [readAxialHoles]);
  useEffect(() => setChamfers(readChamfers), [readChamfers]);
  useEffect(
    () => setCircularPatterns(readCircularPatterns),
    [readCircularPatterns],
  );

  const assumedFields = useMemo(() => {
    const marked = new Set<string>();
    for (const item of assumptions ?? []) marked.add(`${item.path}.${item.field}`);
    return marked;
  }, [assumptions]);

  if (
    !spec ||
    (!readOuter.length &&
      !readBore.length &&
      !readAxialHoles.length &&
      !readChamfers.length &&
      !readCircularPatterns.length)
  ) {
    return null;
  }

  const outerDirty = JSON.stringify(outer) !== JSON.stringify(readOuter);
  const boreDirty = JSON.stringify(bore) !== JSON.stringify(readBore);
  const axialDirty =
    JSON.stringify(axialHoles) !== JSON.stringify(readAxialHoles);
  const chamfersDirty = JSON.stringify(chamfers) !== JSON.stringify(readChamfers);
  const circularDirty =
    JSON.stringify(circularPatterns) !== JSON.stringify(readCircularPatterns);
  const dirty =
    outerDirty || boreDirty || axialDirty || chamfersDirty || circularDirty;
  const expectedChamfers = Math.max(
    expectedChamferCount(spec),
    readChamfers.length,
  );

  const profilesComplete = [...outer, ...bore].every(
    (item) => Number(item.diameter_mm) > 0 && Number(item.length_mm) > 0,
  );

  const axialComplete = axialHoles.every(
    (item) =>
      Number(item.count) > 0 &&
      Number(item.bolt_circle_diameter_mm) > 0 &&
      (item.from_face === "zmin" || item.from_face === "zmax") &&
      typeof item.through === "boolean" &&
      (!(Number(item.entry_offset_mm) > 0) ||
        Number(item.entry_recess_diameter_mm) >
          Number(item.thread?.nominal_diameter_mm)) &&
      (item.through === true ||
        (Number(item.drill_depth_mm ?? item.depth_mm) > 0 &&
          Number(item.thread_depth_mm ?? item.depth_mm) > 0 &&
          Number(item.thread_depth_mm ?? item.depth_mm) <=
            Number(item.drill_depth_mm ?? item.depth_mm))),
  );
  const chamfersComplete =
    chamfers.length >= expectedChamfers &&
    chamfers.every(
      (item) =>
        Number(item.size_mm) > 0 &&
        Number(item.angle_deg) > 0 &&
        Boolean(item.location) &&
        (
          !["shoulder", "bore_mouth"].includes(String(item.location)) ||
          (item.at_z_mm !== null && item.at_z_mm !== undefined) ||
          Number(item.at_diameter_mm) > 0
        ),
    );
  const circularComplete = circularPatterns.every(
    (item) =>
      Number(item.count) > 0 &&
      Number(item.hole_diameter_mm) > 0 &&
      Number(item.bolt_circle_diameter_mm) > 0 &&
      (item.axis_mode === "axial" || item.axis_mode === "inclined") &&
      (item.from_face === "zmin" || item.from_face === "zmax") &&
      Number.isFinite(Number(item.start_angle_deg)) &&
      typeof item.through === "boolean" &&
      (item.through || Number(item.depth_mm) > 0) &&
      (item.axis_mode !== "inclined" ||
        (Number(item.inclination_deg) > 0 &&
          Boolean(item.radial_direction))),
  );
  const rebuildReady =
    profilesComplete && axialComplete && chamfersComplete && circularComplete;
  const readiness = [
    { label: t("vector.spec_editor_check_profile"), ok: profilesComplete },
    { label: t("vector.spec_editor_check_axial"), ok: axialComplete },
    { label: t("vector.spec_editor_check_patterns"), ok: circularComplete },
    { label: t("vector.spec_editor_check_chamfers"), ok: chamfersComplete },
  ];
  const readyCount = readiness.filter((item) => item.ok).length;

  function updateSection(
    group: "outer" | "bore",
    index: number,
    field: "diameter_mm" | "length_mm",
    raw: string,
  ) {
    const value = numberFromInput(raw);
    if (value === undefined) return;
    const setter = group === "outer" ? setOuter : setBore;
    const current = group === "outer" ? outer : bore;
    setter(
      current.map((section, position) =>
        position === index ? { ...section, [field]: value } : section,
      ),
    );
  }

  function updateAxial(
    index: number,
    field: keyof AxialHolePattern,
    value: unknown,
  ) {
    setAxialHoles((current) =>
      current.map((item, position) => {
        if (position !== index) return item;
        if (field === "through" && value === true) {
          return {
            ...item,
            through: true,
            depth_mm: null,
            thread_depth_mm: null,
            drill_depth_mm: null,
          };
        }
        return { ...item, [field]: value };
      }),
    );
  }

  function updateChamfer(index: number, field: keyof Chamfer, value: unknown) {
    setChamfers((current) =>
      current.map((item, position) =>
        position === index ? { ...item, [field]: value } : item,
      ),
    );
  }

  function updateCircular(
    index: number,
    field: keyof CircularHolePattern,
    value: unknown,
  ) {
    setCircularPatterns((current) =>
      current.map((item, position) =>
        position === index ? { ...item, [field]: value } : item,
      ),
    );
  }

  function addChamfer() {
    const exemplar = chamfers[0] ?? readChamfers[0];
    setChamfers((current) => [
      ...current,
      {
        size_mm: exemplar?.size_mm ?? null,
        angle_deg: exemplar?.angle_deg ?? 45,
        location: "shoulder",
        at_z_mm: null,
        at_diameter_mm: null,
      },
    ]);
  }

  async function save(rebuild: boolean) {
    const correction: Record<string, unknown> = {};
    if (outerDirty) correction.outer = outer;
    if (boreDirty) correction.bore = bore;
    if (axialDirty) correction.axial_holes = axialHoles;
    if (chamfersDirty) correction.chamfers = chamfers;
    if (circularDirty) correction.circular_hole_patterns = circularPatterns;
    setSaving(true);
    try {
      await correctSpec(generationId, correction, { rebuild });
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
                  value={shown(section[field])}
                  onChange={(event) =>
                    updateSection(group, index, field, event.target.value)
                  }
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

  const numericInput = (
    value: number | null | undefined,
    onChange: (value: number | null) => void,
    className = "w-24",
  ) => (
    <input
      value={shown(value)}
      inputMode="decimal"
      onChange={(event) => {
        const parsed = numberFromInput(event.target.value);
        if (parsed !== undefined) onChange(parsed);
      }}
      disabled={busy || saving}
      className={`${className} rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-right text-zinc-200`}
    />
  );

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

          <div className="mt-3 rounded border border-white/10 bg-black/20 p-2">
            <div className="flex items-center justify-between gap-2 text-[11px] text-zinc-300">
              <span>
                {t("vector.spec_editor_progress", {
                  done: readyCount,
                  total: readiness.length,
                })}
              </span>
              <span className={rebuildReady ? "text-emerald-300" : "text-amber-300"}>
                {Math.round((readyCount / readiness.length) * 100)}%
              </span>
            </div>
            <div className="mt-2 grid gap-1 sm:grid-cols-2">
              {readiness.map((item) => (
                <div
                  key={item.label}
                  className={`rounded px-2 py-1 text-[11px] ${
                    item.ok
                      ? "bg-emerald-500/10 text-emerald-300"
                      : "bg-amber-500/10 text-amber-200"
                  }`}
                >
                  {item.ok ? "✓" : "○"} {item.label}
                </div>
              ))}
            </div>
          </div>

          {outer.length > 0 && (
            <div className="mt-4">
              <h4 className="mb-1 font-medium text-zinc-200">
                {t("vector.spec_editor_outer_title")}
              </h4>
              <table className="w-full">
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
              </tbody>
              </table>
            </div>
          )}

          {bore.length > 0 && (
            <div className="mt-4">
              <h4 className="mb-1 font-medium text-zinc-200">
                {t("vector.spec_editor_bore_title")}
              </h4>
              <table className="w-full">
                <thead className="text-[11px] text-zinc-500">
                  <tr>
                    <th className="w-8 text-left font-normal">#</th>
                    <th className="text-left font-normal">{t("vector.spec_editor_diameter")}</th>
                    <th className="text-left font-normal">{t("vector.spec_editor_length")}</th>
                    <th className="text-left font-normal">{t("vector.spec_editor_tolerance")}</th>
                  </tr>
                </thead>
                <tbody>{rows("bore", bore)}</tbody>
              </table>
            </div>
          )}

          {axialHoles.length > 0 && (
            <div className="mt-4 border-t border-white/10 pt-3">
              <h4 className="font-medium text-zinc-200">
                {t("vector.spec_editor_axial_title")}
              </h4>
              {axialHoles.map((item, index) => (
                <div
                  key={index}
                  className="mt-2 rounded border border-white/10 bg-black/20 p-2"
                >
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-zinc-400">
                    <span>
                      {String(item.thread?.designation ?? "—")}
                    </span>
                    <span>
                      {t("vector.spec_editor_count")}: {shown(item.count)}
                    </span>
                    <span>
                      {t("vector.spec_editor_pcd")}: ⌀
                      {shown(item.bolt_circle_diameter_mm)}
                    </span>
                  </div>
                  <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_count")}
                      <div className="mt-1">
                        {numericInput(item.count, (value) => updateAxial(index, "count", value), "w-full")}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_pcd")}
                      <div className="mt-1">
                        {numericInput(item.bolt_circle_diameter_mm, (value) => updateAxial(index, "bolt_circle_diameter_mm", value), "w-full")}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_from_face")}
                      <select
                        value={item.from_face ?? ""}
                        onChange={(event) =>
                          updateAxial(
                            index,
                            "from_face",
                            event.target.value || null,
                          )
                        }
                        disabled={busy || saving}
                        className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-zinc-200"
                      >
                        <option value="">{t("vector.spec_editor_unknown")}</option>
                        <option value="zmin">{t("vector.spec_editor_zmin")}</option>
                        <option value="zmax">{t("vector.spec_editor_zmax")}</option>
                      </select>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_entry_offset")}
                      <div className="mt-1">
                        {numericInput(
                          item.entry_offset_mm,
                          (value) => updateAxial(index, "entry_offset_mm", value),
                          "w-full",
                        )}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_entry_recess_diameter")}
                      <div className="mt-1">
                        {numericInput(
                          item.entry_recess_diameter_mm,
                          (value) =>
                            updateAxial(index, "entry_recess_diameter_mm", value),
                          "w-full",
                        )}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_execution")}
                      <select
                        value={
                          typeof item.through === "boolean"
                            ? item.through
                              ? "through"
                              : "blind"
                            : ""
                        }
                        onChange={(event) =>
                          updateAxial(
                            index,
                            "through",
                            event.target.value === ""
                              ? null
                              : event.target.value === "through",
                          )
                        }
                        disabled={busy || saving}
                        className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-zinc-200"
                      >
                        <option value="">{t("vector.spec_editor_unknown")}</option>
                        <option value="through">
                          {t("vector.spec_editor_through")}
                        </option>
                        <option value="blind">
                          {t("vector.spec_editor_blind")}
                        </option>
                      </select>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_thread_depth")}
                      <div className="mt-1">
                        {numericInput(
                          item.thread_depth_mm ?? item.depth_mm,
                          (value) =>
                            updateAxial(index, "thread_depth_mm", value),
                          "w-full",
                        )}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_drill_depth")}
                      <div className="mt-1">
                        {numericInput(
                          item.drill_depth_mm ?? item.depth_mm,
                          (value) =>
                            updateAxial(index, "drill_depth_mm", value),
                          "w-full",
                        )}
                      </div>
                    </label>
                  </div>
                  {Number(item.thread?.nominal_diameter_mm) === 8 && (
                    <p className="mt-2 text-[11px] text-amber-300/80">
                      {t("vector.spec_editor_m8_geometry")}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}

          {circularPatterns.length > 0 && (
            <div className="mt-4 border-t border-white/10 pt-3">
              <h4 className="font-medium text-zinc-200">
                {t("vector.spec_editor_circular_patterns_title")}
              </h4>
              <p className="mt-1 text-[11px] text-amber-300/80">
                {t("vector.spec_editor_circular_patterns_hint")}
              </p>
              {circularPatterns.map((item, index) => (
                <div
                  key={index}
                  className="mt-2 rounded border border-white/10 bg-black/20 p-2"
                >
                  <div className="text-zinc-300">
                    {shown(item.count)}×⌀{shown(item.hole_diameter_mm)} · PCD ⌀
                    {shown(item.bolt_circle_diameter_mm)} · {String(item.axis_mode ?? "—")}
                  </div>
                  <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_count")}
                      <div className="mt-1">
                        {numericInput(item.count, (value) => updateCircular(index, "count", value), "w-full")}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_diameter")}
                      <div className="mt-1">
                        {numericInput(item.hole_diameter_mm, (value) => updateCircular(index, "hole_diameter_mm", value), "w-full")}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_pcd")}
                      <div className="mt-1">
                        {numericInput(item.bolt_circle_diameter_mm, (value) => updateCircular(index, "bolt_circle_diameter_mm", value), "w-full")}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_axis_mode")}
                      <select
                        value={item.axis_mode ?? ""}
                        onChange={(event) =>
                          updateCircular(index, "axis_mode", event.target.value || undefined)
                        }
                        disabled={busy || saving}
                        className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-zinc-200"
                      >
                        <option value="">{t("vector.spec_editor_unknown")}</option>
                        <option value="axial">{t("vector.spec_editor_axis_axial")}</option>
                        <option value="inclined">{t("vector.spec_editor_axis_inclined")}</option>
                      </select>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_from_face")}
                      <select
                        value={item.from_face ?? ""}
                        onChange={(event) =>
                          updateCircular(
                            index,
                            "from_face",
                            event.target.value || null,
                          )
                        }
                        disabled={busy || saving}
                        className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-zinc-200"
                      >
                        <option value="">{t("vector.spec_editor_unknown")}</option>
                        <option value="zmin">{t("vector.spec_editor_zmin")}</option>
                        <option value="zmax">{t("vector.spec_editor_zmax")}</option>
                      </select>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_start_angle")}
                      <div className="mt-1">
                        {numericInput(
                          item.start_angle_deg,
                          (value) => updateCircular(index, "start_angle_deg", value),
                          "w-full",
                        )}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_execution")}
                      <select
                        value={typeof item.through === "boolean" ? (item.through ? "through" : "blind") : ""}
                        onChange={(event) =>
                          updateCircular(
                            index,
                            "through",
                            event.target.value === "" ? null : event.target.value === "through",
                          )
                        }
                        disabled={busy || saving}
                        className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-zinc-200"
                      >
                        <option value="">{t("vector.spec_editor_unknown")}</option>
                        <option value="through">{t("vector.spec_editor_through")}</option>
                        <option value="blind">{t("vector.spec_editor_blind")}</option>
                      </select>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_depth")}
                      <div className="mt-1">
                        {numericInput(
                          item.depth_mm,
                          (value) => updateCircular(index, "depth_mm", value),
                          "w-full",
                        )}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_inclination")}
                      <div className="mt-1">
                        {numericInput(
                          item.inclination_deg,
                          (value) => updateCircular(index, "inclination_deg", value),
                          "w-full",
                        )}
                      </div>
                    </label>
                    <label className="text-zinc-500">
                      {t("vector.spec_editor_radial_direction")}
                      <select
                        value={item.radial_direction ?? ""}
                        onChange={(event) =>
                          updateCircular(
                            index,
                            "radial_direction",
                            event.target.value || null,
                          )
                        }
                        disabled={busy || saving || item.axis_mode !== "inclined"}
                        className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-zinc-200 disabled:opacity-40"
                      >
                        <option value="">{t("vector.spec_editor_unknown")}</option>
                        <option value="outward">
                          {t("vector.spec_editor_outward")}
                        </option>
                        <option value="inward">
                          {t("vector.spec_editor_inward")}
                        </option>
                      </select>
                    </label>
                    {item.axis_mode === "inclined" && (
                      <label className="text-zinc-500">
                        {t("vector.spec_editor_connection_station")}
                        <div className="mt-1">
                          {numericInput(
                            item.connection_station_mm,
                            (value) => updateCircular(index, "connection_station_mm", value),
                            "w-full",
                          )}
                        </div>
                      </label>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}

          {(readChamfers.length > 0 || expectedChamfers > 0) && (
            <div className="mt-4 border-t border-white/10 pt-3">
              <div className="flex items-center justify-between gap-2">
                <h4 className="font-medium text-zinc-200">
                  {t("vector.spec_editor_chamfers_title")}
                </h4>
                <span
                  className={
                    chamfers.length >= expectedChamfers
                      ? "text-emerald-400"
                      : "text-amber-400"
                  }
                >
                  {t("vector.spec_editor_chamfers_progress", {
                    actual: chamfers.length,
                    expected: expectedChamfers,
                  })}
                </span>
              </div>
              {chamfers.map((item, index) => (
                <div
                  key={index}
                  className="mt-2 grid grid-cols-1 gap-2 rounded border border-white/10 bg-black/20 p-2 sm:grid-cols-2 lg:grid-cols-3"
                >
                  <label className="text-zinc-500">
                    {t("vector.spec_editor_size")}
                    <div className="mt-1">
                      {numericInput(
                        item.size_mm,
                        (value) => updateChamfer(index, "size_mm", value),
                        "w-full",
                      )}
                    </div>
                  </label>
                  <label className="text-zinc-500">
                    {t("vector.spec_editor_angle")}
                    <div className="mt-1">
                      {numericInput(
                        item.angle_deg,
                        (value) => updateChamfer(index, "angle_deg", value),
                        "w-full",
                      )}
                    </div>
                  </label>
                  <label className="col-span-2 text-zinc-500">
                    {t("vector.spec_editor_location")}
                    <select
                      value={item.location ?? ""}
                      onChange={(event) =>
                        updateChamfer(index, "location", event.target.value)
                      }
                      disabled={busy || saving}
                      className="mt-1 w-full rounded border border-white/10 bg-zinc-900 px-1.5 py-1 text-zinc-200"
                    >
                      <option value="">{t("vector.spec_editor_unknown")}</option>
                      <option value="left_end">
                        {t("vector.spec_editor_left_end")}
                      </option>
                      <option value="right_end">
                        {t("vector.spec_editor_right_end")}
                      </option>
                      <option value="shoulder">
                        {t("vector.spec_editor_shoulder")}
                      </option>
                      <option value="bore_mouth">
                        {t("vector.spec_editor_bore_mouth")}
                      </option>
                    </select>
                  </label>
                  <label className="text-zinc-500">
                    {t("vector.spec_editor_at_z")}
                    <div className="mt-1">
                      {numericInput(
                        item.at_z_mm,
                        (value) => updateChamfer(index, "at_z_mm", value),
                        "w-full",
                      )}
                    </div>
                  </label>
                  <label className="text-zinc-500">
                    {t("vector.spec_editor_at_diameter")}
                    <div className="mt-1 flex gap-1">
                      {numericInput(
                        item.at_diameter_mm,
                        (value) =>
                          updateChamfer(index, "at_diameter_mm", value),
                        "w-full",
                      )}
                      <button
                        type="button"
                        onClick={() =>
                          setChamfers((current) =>
                            current.filter((_, position) => position !== index),
                          )
                        }
                        disabled={busy || saving}
                        className="rounded border border-red-500/30 px-2 text-red-300"
                        title={t("vector.spec_editor_remove")}
                      >
                        ×
                      </button>
                    </div>
                  </label>
                </div>
              ))}
              <button
                type="button"
                onClick={addChamfer}
                disabled={busy || saving}
                className="mt-2 rounded border border-white/15 px-2 py-1 text-zinc-300 hover:bg-white/5"
              >
                + {t("vector.spec_editor_add_chamfer")}
              </button>
            </div>
          )}

          {!rebuildReady && (
            <p className="mt-3 rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
              {t("vector.spec_editor_rebuild_blocked")}
            </p>
          )}

          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!dirty || busy || saving}
              onClick={() => save(true)}
              className={`rounded px-2 py-1 text-white disabled:opacity-40 ${
                rebuildReady
                  ? "bg-emerald-600 hover:bg-emerald-500"
                  : "bg-amber-600 hover:bg-amber-500"
              }`}
            >
              {saving
                ? t("vector.spec_editor_saving")
                : rebuildReady
                  ? t("vector.spec_editor_rebuild")
                  : t("vector.spec_editor_rebuild_preview")}
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
