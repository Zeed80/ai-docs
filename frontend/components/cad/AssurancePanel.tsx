"use client";

import { useMemo } from "react";

import type {
  SpecAssumption,
  SpecConsensus,
  SpecCrossCheck,
  SpecDimensionCheck,
  SpecFollowup,
  Solid3dSummary,
} from "@/lib/studio-api";

/** What the digitization actually established, and what it did not.
 *
 * All of this was computed and shown to nobody: the cross-check against the
 * image, whether that comparison even ran, which read callouts made it onto the
 * drawing, which values were completed rather than read. Silence about a check
 * that did not happen reads exactly like a check that passed — which is the
 * failure mode this panel exists to remove.
 *
 * It reports; it does not gate. Blocking lives in ValidationPanel and in the
 * acceptance rules. */
export default function AssurancePanel({
  crosscheck,
  dimensionCheck,
  assumptions,
  followups,
  consensus,
  solid,
  onCorrectSpec,
  t,
}: {
  crosscheck?: SpecCrossCheck;
  dimensionCheck?: SpecDimensionCheck;
  assumptions?: SpecAssumption[];
  followups?: SpecFollowup[];
  consensus?: SpecConsensus;
  solid?: Solid3dSummary;
  onCorrectSpec?: () => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const errors = useMemo(
    () => (crosscheck?.findings ?? []).filter((f) => f.severity === "error"),
    [crosscheck],
  );
  const warnings = useMemo(
    () => (crosscheck?.findings ?? []).filter((f) => f.severity !== "error"),
    [crosscheck],
  );
  const refusedFollowups = (followups ?? []).filter((f) => !f.accepted);
  const acceptedFollowups = (followups ?? []).filter((f) => f.accepted);
  const rasterRan = crosscheck?.raster_check === "checked";
  const solidVerified = Boolean(
    (solid?.verification as { ok?: boolean } | undefined)?.ok &&
      solid?.build_status === "verified",
  );
  const sheetVerified = Boolean(
    (solid?.sheet?.verification as { ok?: boolean } | undefined)?.ok,
  );

  const nothingToShow =
    !crosscheck &&
    !dimensionCheck &&
    !(assumptions ?? []).length &&
    !(followups ?? []).length &&
    !consensus &&
    !solid;
  if (nothingToShow) return null;

  return (
    <section className="rounded border border-white/10 bg-zinc-950/60 p-3 text-xs">
      <h3 className="mb-2 text-sm font-medium text-zinc-200">
        {t("vector.assurance_title")}
      </h3>

      <ul className="space-y-1.5">
        {consensus?.passes ? (
          <Row
            ok={(consensus.disagreements ?? []).length === 0}
            label={t("vector.assurance_consensus", {
              passes: consensus.passes,
              usable: consensus.usable ?? consensus.passes,
            })}
            detail={(consensus.disagreements ?? []).join("; ")}
          />
        ) : null}

        {crosscheck ? (
          <Row
            ok={errors.length === 0}
            neutral={!rasterRan}
            label={
              rasterRan
                ? t("vector.assurance_raster_checked")
                : t("vector.assurance_raster_skipped")
            }
            detail={
              rasterRan
                ? t("vector.assurance_circles", {
                    count: crosscheck.measured_circles,
                  })
                : t("vector.assurance_raster_skipped_hint")
            }
          />
        ) : null}

        {errors.map((finding) => (
          <Row key={finding.code} ok={false} label={finding.message} />
        ))}
        {warnings.map((finding) => (
          <Row key={finding.code} neutral label={finding.message} />
        ))}

        {solid ? (
          <>
            <Row
              ok={Boolean(solid.built) && solidVerified}
              label={
                solid.built
                  ? t("vector.assurance_solid_built")
                  : t("vector.assurance_solid_failed")
              }
              detail={
                solid.built
                  ? [
                      solid.sheet?.sheet_format,
                      solid.sheet?.scale,
                      solid.sheet?.views?.join(" + "),
                      solid.build_status,
                      sheetVerified ? t("vector.assurance_views_match") : "",
                    ]
                      .filter(Boolean)
                      .join(" · ")
                  : solid.error
              }
            />
            {(solid.sheet?.view_reasons ?? []).map((view) => (
              <Row
                key={`${view.view_index}-${view.kind}`}
                ok={view.visible !== false}
                neutral={view.visible === false}
                label={t("vector.assurance_view_reason", {
                  view: view.kind ?? "—",
                })}
                detail={view.reason}
              />
            ))}
          </>
        ) : null}

        {solid?.source_projection_verification ? (
          <Row
            ok={Boolean(solid.source_projection_verification.ok)}
            label={t("vector.assurance_source_projection")}
            detail={[
              solid.source_projection_verification.status,
              typeof solid.source_projection_verification.score === "number"
                ? `${Math.round(solid.source_projection_verification.score * 100)}%`
                : "",
              ...(solid.source_projection_verification.missing_evidence ?? []),
            ]
              .filter(Boolean)
              .join(" · ")}
          />
        ) : null}

        {dimensionCheck ? (
          <Row
            ok={dimensionCheck.status === "ok"}
            neutral={dimensionCheck.status !== "ok"}
            label={t("vector.assurance_callouts", {
              placed: dimensionCheck.placed ?? 0,
              read: dimensionCheck.read ?? 0,
            })}
            detail={(dimensionCheck.unplaced ?? []).join(", ")}
          />
        ) : null}

        {acceptedFollowups.length > 0 ? (
          <Row
            neutral
            label={t("vector.assurance_followup_accepted", {
              count: acceptedFollowups.length,
            })}
            detail={acceptedFollowups.map((f) => f.path).join(", ")}
          />
        ) : null}
        {refusedFollowups.length > 0 ? (
          <Row
            neutral
            label={t("vector.assurance_followup_refused", {
              count: refusedFollowups.length,
            })}
            detail={refusedFollowups
              .map((f) => `${f.path}: ${f.reason}`)
              .join("; ")}
          />
        ) : null}
      </ul>

      {(assumptions ?? []).length > 0 && (
        <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/5 p-2">
          <p className="font-medium text-amber-300">
            {t("vector.assurance_assumed_title", {
              count: (assumptions ?? []).length,
            })}
          </p>
          <p className="mt-0.5 text-[11px] text-amber-200/70">
            {t("vector.assurance_assumed_hint")}
          </p>
          <ul className="mt-1.5 space-y-1">
            {(assumptions ?? []).map((item) => (
              <li key={`${item.path}.${item.field}`} className="text-zinc-300">
                <span className="font-mono text-[11px] text-zinc-400">
                  {item.path}.{item.field}
                </span>{" "}
                = {item.value_mm} мм
                <span className="text-zinc-500"> — {item.rule}</span>
              </li>
            ))}
          </ul>
          {onCorrectSpec && (
            <button
              type="button"
              onClick={onCorrectSpec}
              className="mt-2 rounded border border-amber-400/40 px-2 py-1 text-[11px] text-amber-200 hover:bg-amber-400/10"
            >
              {t("vector.assurance_fix_spec")}
            </button>
          )}
        </div>
      )}
    </section>
  );
}

function Row({
  ok,
  neutral,
  label,
  detail,
}: {
  ok?: boolean;
  neutral?: boolean;
  label: string;
  detail?: string;
}) {
  const mark = neutral ? "•" : ok ? "✓" : "✕";
  const colour = neutral
    ? "text-zinc-400"
    : ok
      ? "text-emerald-400"
      : "text-red-400";
  return (
    <li className="flex gap-2">
      <span className={`${colour} shrink-0`}>{mark}</span>
      <span className="text-zinc-300">
        {label}
        {detail ? <span className="text-zinc-500"> — {detail}</span> : null}
      </span>
    </li>
  );
}
