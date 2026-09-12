"use client";

import { useEffect, useMemo, useState } from "react";

import { correctSpec, type SpecVerification } from "@/lib/studio-api";

type Hole = {
  center_x_mm?: number | null;
  center_y_mm?: number | null;
  diameter_mm?: number | null;
  [key: string]: unknown;
};

type Pattern = {
  kind?: string | null;
  count?: number | null;
  bolt_circle_diameter_mm?: number | null;
  hole_diameter_mm?: number | null;
  start_angle_deg?: number | null;
  [key: string]: unknown;
};

type Item = SpecVerification["items"][number];

function parse(raw: string): number | null | undefined {
  if (raw.trim() === "") return null;
  const value = Number(raw.replace(",", "."));
  return Number.isFinite(value) ? value : undefined;
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

/** Holes of a flat part (plate or flange) — the one geometry the spec editor
 * could not correct at all, although the verify stage measures exactly it.
 *
 * Plate coordinates are shown the way the sheet dimensions them — from the left
 * and bottom edge — and converted back to the spec's centred system on save.
 * Where the sheet measurement disagrees with the reading, the measured value
 * sits next to the field with a one-click "take it": the reading is never
 * replaced silently, the person decides. The whole profile goes back as one
 * correction (the backend replaces `main_view.profile` wholesale). */
export default function ProfileHolesEditor({
  generationId,
  spec,
  verification,
  busy,
  onDone,
  onError,
  t,
}: {
  generationId: string;
  spec: Record<string, unknown> | undefined;
  verification?: SpecVerification;
  busy: boolean;
  onDone: (result?: { rebuild_task_id?: string | null }) => void;
  onError: (message: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const profile = useMemo(
    () =>
      ((spec?.main_view as Record<string, unknown> | undefined)?.profile ??
        {}) as Record<string, unknown>,
    [spec],
  );
  const readHoles = useMemo(
    () => ((profile.holes ?? []) as Hole[]).map((item) => ({ ...item })),
    [profile],
  );
  const readPatterns = useMemo(
    () =>
      ((profile.hole_patterns ?? []) as Pattern[]).map((item) => ({ ...item })),
    [profile],
  );
  const [holes, setHoles] = useState<Hole[]>(readHoles);
  const [patterns, setPatterns] = useState<Pattern[]>(readPatterns);
  const [saving, setSaving] = useState(false);
  useEffect(() => setHoles(readHoles), [readHoles]);
  useEffect(() => setPatterns(readPatterns), [readPatterns]);

  const shape = profile.shape;
  if (
    !spec ||
    (shape !== "rectangle" && shape !== "circle") ||
    (!readHoles.length && !readPatterns.length)
  ) {
    return null;
  }

  const width = Number(profile.width_mm) || 0;
  const height = Number(profile.height_mm) || 0;
  const fromEdges = shape === "rectangle" && width > 0 && height > 0;
  const offset = {
    x: fromEdges ? width / 2 : 0,
    y: fromEdges ? height / 2 : 0,
  };
  const dirty =
    JSON.stringify(holes) !== JSON.stringify(readHoles) ||
    JSON.stringify(patterns) !== JSON.stringify(readPatterns);
  const verdictFor = (path: string): Item | undefined =>
    verification?.items.find((item) => item.path === path);
  const disabled = busy || saving;

  function setHole(index: number, field: keyof Hole, value: number | null) {
    setHoles((current) =>
      current.map((item, position) =>
        position === index ? { ...item, [field]: value } : item,
      ),
    );
  }

  function setPattern(
    index: number,
    field: keyof Pattern,
    value: number | null,
  ) {
    setPatterns((current) =>
      current.map((item, position) =>
        position === index ? { ...item, [field]: value } : item,
      ),
    );
  }

  async function save(rebuild: boolean) {
    setSaving(true);
    try {
      const result = await correctSpec(
        generationId,
        { profile: { ...profile, holes, hole_patterns: patterns } },
        { rebuild },
      );
      onDone(result);
    } catch (error) {
      onError(String((error as Error).message || error));
    } finally {
      setSaving(false);
    }
  }

  /** One numeric field: display = spec value + shift; measured shown when it differs. */
  function field(
    label: string,
    value: number | null | undefined,
    shift: number,
    measured: number | null | undefined,
    tolerance: number | undefined,
    apply: (specValue: number | null) => void,
    period?: number,
  ) {
    const display =
      value === null || value === undefined ? "" : String(round(value + shift));
    const measuredDisplay =
      measured === null || measured === undefined
        ? null
        : round(measured + shift);
    // Замер показывается, только если элемент опровергнут и именно это поле
    // разошлось больше допуска стадии: Ø 6,70 против 6,6 при допуске 0,3 — не
    // расхождение, а шум. Фаза сравнивается по модулю шага массива.
    let gap = Number.POSITIVE_INFINITY;
    if (
      measured !== null &&
      measured !== undefined &&
      value !== null &&
      value !== undefined
    ) {
      gap = Math.abs(measured - value);
      if (period) {
        gap %= period;
        gap = Math.min(gap, period - gap);
      }
    }
    const differs =
      measuredDisplay !== null && tolerance !== undefined && gap > tolerance;
    return (
      <label className="text-zinc-400">
        {label}
        <input
          aria-label={label}
          value={display}
          onChange={(event) => {
            const parsed = parse(event.target.value);
            if (parsed !== undefined)
              apply(parsed === null ? null : parsed - shift);
          }}
          disabled={disabled}
          className={`mt-1 w-full rounded border bg-zinc-900 px-1.5 py-0.5 text-right text-zinc-200 ${
            differs ? "border-rose-500/60" : "border-white/10"
          }`}
        />
        {differs && (
          <span className="mt-0.5 flex items-center gap-1 text-[11px] text-rose-300">
            {t("vector.profile_editor_measured", { value: measuredDisplay! })}
            <button
              type="button"
              onClick={() => apply(measured!)}
              disabled={disabled}
              className="rounded border border-rose-400/40 px-1 text-rose-200 hover:bg-rose-500/10"
            >
              {t("vector.profile_editor_take")}
            </button>
          </span>
        )}
      </label>
    );
  }

  return (
    <section className="rounded border border-white/10 bg-zinc-950/60 p-3 text-xs">
      <h3 className="text-sm font-medium text-zinc-200">
        {t("vector.profile_editor_title")}
      </h3>
      <p className="mt-1 text-[11px] text-zinc-400">
        {fromEdges
          ? t("vector.profile_editor_hint")
          : t("vector.profile_editor_hint_circle")}
      </p>

      {holes.map((hole, index) => {
        const verdict = verdictFor(`main_view.profile.holes[${index}]`);
        const measured = verdict?.measured ?? {};
        const tol =
          verdict?.status === "refuted" ? (verdict.tolerance_mm ?? {}) : null;
        return (
          <div
            key={`hole-${index}`}
            className="mt-2 rounded border border-white/10 bg-black/20 p-2"
          >
            <div className="text-zinc-300">
              {t("vector.profile_editor_hole", { index: index + 1 })}
            </div>
            <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-3">
              {field(
                fromEdges
                  ? t("vector.profile_editor_x_left")
                  : t("vector.profile_editor_x_centre"),
                hole.center_x_mm,
                offset.x,
                measured.center_x_mm,
                tol?.position,
                (value) => setHole(index, "center_x_mm", value),
              )}
              {field(
                fromEdges
                  ? t("vector.profile_editor_y_bottom")
                  : t("vector.profile_editor_y_centre"),
                hole.center_y_mm,
                offset.y,
                measured.center_y_mm,
                tol?.position,
                (value) => setHole(index, "center_y_mm", value),
              )}
              {field(
                t("vector.spec_editor_diameter"),
                hole.diameter_mm,
                0,
                measured.diameter_mm,
                tol?.diameter,
                (value) => setHole(index, "diameter_mm", value),
              )}
            </div>
          </div>
        );
      })}

      {patterns.map((pattern, index) => {
        const verdict = verdictFor(`main_view.profile.hole_patterns[${index}]`);
        const measured = verdict?.measured ?? {};
        const tol =
          verdict?.status === "refuted" ? (verdict.tolerance_mm ?? {}) : null;
        const period =
          360 / Math.max(1, Number(measured.count ?? pattern.count) || 1);
        return (
          <div
            key={`pattern-${index}`}
            className="mt-2 rounded border border-white/10 bg-black/20 p-2"
          >
            <div className="text-zinc-300">
              {t("vector.profile_editor_bolt_circle", { index: index + 1 })}
            </div>
            <div className="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
              {field(
                t("vector.spec_editor_count"),
                pattern.count,
                0,
                measured.count,
                tol?.count,
                (value) => setPattern(index, "count", value),
              )}
              {field(
                t("vector.spec_editor_pcd"),
                pattern.bolt_circle_diameter_mm,
                0,
                measured.bolt_circle_diameter_mm,
                tol?.pcd,
                (value) => setPattern(index, "bolt_circle_diameter_mm", value),
              )}
              {field(
                t("vector.spec_editor_diameter"),
                pattern.hole_diameter_mm,
                0,
                measured.hole_diameter_mm,
                tol?.diameter,
                (value) => setPattern(index, "hole_diameter_mm", value),
              )}
              {field(
                t("vector.profile_editor_phase"),
                pattern.start_angle_deg,
                0,
                measured.start_angle_deg,
                tol?.phase,
                (value) => setPattern(index, "start_angle_deg", value),
                period,
              )}
            </div>
          </div>
        );
      })}

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => save(true)}
          disabled={disabled || !dirty}
          className="rounded bg-sky-600 px-2 py-1 text-white disabled:opacity-40"
        >
          {t("vector.spec_editor_rebuild")}
        </button>
        <button
          type="button"
          onClick={() => save(false)}
          disabled={disabled || !dirty}
          className="rounded border border-white/15 px-2 py-1 text-zinc-200 disabled:opacity-40"
        >
          {t("vector.spec_editor_save_only")}
        </button>
      </div>
    </section>
  );
}
