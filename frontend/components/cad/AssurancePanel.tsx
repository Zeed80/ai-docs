"use client";

import { useMemo } from "react";

import { getApiBaseUrl } from "@/lib/api-base";

import type {
  SpecAssumption,
  SpecConsensus,
  SpecCrossCheck,
  SpecDimensionCheck,
  SpecFollowup,
  SpecVerification,
  Solid3dSummary,
} from "@/lib/studio-api";

const VERIFY_KINDS = new Set([
  "plate_hole",
  "bolt_circle",
  "concentric_hole",
  "shaft_step",
  "keyway",
  "cross_hole",
  "groove",
  "chamfer",
  // Корпуса (Ф5): элементы граней и толщина, измеренная по видам листа.
  "wall_feature",
  "plate_thickness",
  // Листовая деталь (X4): форма сечения — число, направления и углы гибов.
  "bent_section",
]);

/** Вырез листа вокруг элемента проверки с обведённой рамкой (Ф9). Индекс —
 *  позиция в `spec_verification.items`, как её хранит сервер. */
export function verificationOverlayUrl(generationId: string, index: number): string {
  return `${getApiBaseUrl()}/api/image-gen/${generationId}/verification/${index}/overlay`;
}

/** «Отверстие 3» из `main_view.profile.holes[2]` — номер элемента с единицы. */
function verifyElement(
  item: SpecVerification["items"][number],
  t: (k: string, v?: Record<string, string | number>) => string,
): string {
  if (!VERIFY_KINDS.has(item.kind)) return item.path;
  const index = Number(/\[(\d+)\]$/.exec(item.path)?.[1] ?? 0) + 1;
  return t(`vector.assurance_verify_kind_${item.kind}`, { index });
}

const FIELD_KEYS = new Set([
  "diameter_mm",
  "length_mm",
  "width_mm",
  "height_mm",
  "thickness_mm",
  "center_u_mm",
  "center_v_mm",
  "bolt_circle_diameter_mm",
  "hole_diameter_mm",
]);

/** Подтверждено из проверенного — по видам элементов, в порядке появления.
 *  Одна общая строка «подтверждено 12» не говорит, что все 12 — ступени, а оба
 *  паза остались непроверенными. */
export function verifyByKind(
  items: SpecVerification["items"],
): { kind: string; confirmed: number; checked: number }[] {
  const order: string[] = [];
  const counts = new Map<string, { confirmed: number; checked: number }>();
  for (const item of items) {
    if (!VERIFY_KINDS.has(item.kind)) continue;
    if (!counts.has(item.kind)) {
      order.push(item.kind);
      counts.set(item.kind, { confirmed: 0, checked: 0 });
    }
    const entry = counts.get(item.kind)!;
    entry.checked += 1;
    if (item.status === "confirmed") entry.confirmed += 1;
  }
  return order.map((kind) => ({ kind, ...counts.get(kind)! }));
}

/** «длина» из `length_mm`: имя поля спека — оператору не язык. */
function fieldLabel(
  field: string,
  t: (k: string, v?: Record<string, string | number>) => string,
): string {
  return FIELD_KEYS.has(field) ? t(`vector.assurance_field_${field}`) : field;
}

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
  verification,
  generationId,
  t,
}: {
  crosscheck?: SpecCrossCheck;
  verification?: SpecVerification;
  dimensionCheck?: SpecDimensionCheck;
  assumptions?: SpecAssumption[];
  followups?: SpecFollowup[];
  consensus?: SpecConsensus;
  solid?: Solid3dSummary;
  /** Для выреза листа у опровергнутого и неизмеримого; без него — только текст. */
  generationId?: string;
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
  // A2/UX: solid.assumptions carries the 3D build's own human-readable
  // notes AHEAD of raw "critical assertion <id>" entries (see
  // cad_emg_compat.feature_tree_from_graph/feature_tree_revision_patch) —
  // the latter name WHICH assertion is unconfirmed, not something a person
  // reads, so they are filtered out of this panel rather than shown as if
  // they were equally actionable.
  const buildAssumptions = useMemo(
    () =>
      (solid?.assumptions ?? []).filter(
        (item) => !item.startsWith("critical assertion "),
      ),
    [solid?.assumptions],
  );

  const nothingToShow =
    !crosscheck &&
    !dimensionCheck &&
    !(assumptions ?? []).length &&
    !buildAssumptions.length &&
    !(followups ?? []).length &&
    !consensus &&
    !solid &&
    !verification?.items?.length;
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

        {/* Прочитанное против самого листа: подтверждённое — одной строкой
            сводки, опровергнутое и неизмеримое — поимённо, с причиной. */}
        {verification?.items?.length ? (
          <>
            <Row
              ok={
                verification.summary.refuted === 0 &&
                verification.summary.confirmed > 0
              }
              neutral={
                verification.summary.confirmed === 0 &&
                verification.summary.refuted === 0
              }
              label={t("vector.assurance_verify", {
                confirmed: verification.summary.confirmed,
                refuted: verification.summary.refuted,
                unmeasurable: verification.summary.unmeasurable,
              })}
            />
            {verifyByKind(verification.items).length > 1 ? (
              <Row
                neutral
                label={t("vector.assurance_verify_by_kind", {
                  list: verifyByKind(verification.items)
                    .map((group) =>
                      t("vector.assurance_verify_group", {
                        name: t(`vector.assurance_verify_group_${group.kind}`),
                        confirmed: group.confirmed,
                        checked: group.checked,
                      }),
                    )
                    .join(" · "),
                })}
              />
            ) : null}
            {verification.items
              .map((item, index) => ({ item, index }))
              .filter(({ item }) => item.status !== "confirmed")
              .map(({ item, index }) => (
                <Row
                  key={`${item.kind}-${item.path}`}
                  ok={false}
                  neutral={item.status === "unmeasurable"}
                  label={t(`vector.assurance_verify_${item.status}`, {
                    element: verifyElement(item, t),
                  })}
                  detail={item.reason}
                  overlay={
                    generationId && item.evidence_bbox_px
                      ? {
                          src: verificationOverlayUrl(generationId, index),
                          alt: t("vector.assurance_verify_overlay", {
                            element: verifyElement(item, t),
                          }),
                        }
                      : undefined
                  }
                />
              ))}
            {/* Профиль вала собран по листу вместо прочитанного целиком. */}
            {verification.profile_adoption ? (
              <Row
                ok
                label={t("vector.assurance_profile_adopted", {
                  steps: verification.profile_adoption.value
                    .map(
                      (step) =>
                        `${step.thread?.designation ?? `Ø${step.diameter_mm}`}×${step.length_mm}`,
                    )
                    .join(" · "),
                })}
                detail={verification.profile_adoption.reason}
              />
            ) : null}
            {/* Контур пластины собран по листу вместо прочитанного. */}
            {verification.contour_adoption ? (
              <Row
                ok
                label={t("vector.assurance_contour_adopted", {
                  width: verification.contour_adoption.value.width_mm,
                  height: verification.contour_adoption.value.height_mm,
                  holes: verification.contour_adoption.value.holes?.length ?? 0,
                })}
                detail={verification.contour_adoption.reason}
              />
            ) : null}
            {verification.sleeve_adoption ? (
              <Row
                ok
                label={t("vector.assurance_sleeve_adopted", {
                  outer: verification.sleeve_adoption.value.outer
                    .map((step) => `Ø${step.diameter_mm}×${step.length_mm}`)
                    .join(" · "),
                  bore: verification.sleeve_adoption.value.bore
                    .map((step) => `Ø${step.diameter_mm}×${step.length_mm}`)
                    .join(" · "),
                })}
                detail={verification.sleeve_adoption.reason}
              />
            ) : null}
            {/* Пазы, найденные на листе и не выписанные ридером. */}
            {(verification.keyway_additions ?? []).map((addition) => (
              <Row
                key={`keyway-added-${addition.axial_start_mm}`}
                ok
                label={t("vector.assurance_keyway_added", {
                  start: addition.axial_start_mm,
                  end: addition.axial_start_mm + addition.length_mm,
                  width: addition.width_mm,
                  depth: addition.depth_mm,
                })}
                detail={addition.reason}
              />
            ))}
            {/* Согласование: принятое по листу — с прежним прочитанным,
                спорное — оба варианта, решение за человеком. */}
            {(verification.reconciliation ?? []).map((decision) => (
              <Row
                key={`reconcile-${decision.path}-${decision.field}`}
                ok={decision.action === "adopt"}
                label={
                  decision.action === "adopt"
                    ? t("vector.assurance_reconcile_adopted", {
                        element: verifyElement(
                          {
                            ...decision,
                            read: {},
                            measured: {},
                            status: "confirmed",
                          },
                          t,
                        ),
                        field: fieldLabel(decision.field, t),
                        read: decision.read,
                        value: decision.value ?? decision.measured,
                      })
                    : t("vector.assurance_reconcile_ask", {
                        element: verifyElement(
                          {
                            ...decision,
                            read: {},
                            measured: {},
                            status: "refuted",
                          },
                          t,
                        ),
                        field: fieldLabel(decision.field, t),
                        read: decision.read,
                        measured: decision.measured,
                      })
                }
                detail={decision.reason}
              />
            ))}
          </>
        ) : null}

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
              ...(solid.source_projection_verification.paired_comparison
                ?.issues ?? []),
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
                <span className="text-zinc-400"> — {item.rule}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* A2/UX: solid_3d.assumptions — the 3D build's OWN provisional
          values (an averaged step length, an omitted bore) — computed and
          shown to nobody until now, same failure mode the panel above this
          one already exists to remove. A real value from a real live run
          reads like "0:outer:4: длина ступени Ø29.5 не указана —
          построено с предположением 29.17 мм..." — long, but it IS the
          actual answer to "what did the system guess and why", not a raw
          assertion id. */}
      {(buildAssumptions ?? []).length > 0 && (
        <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/5 p-2">
          <p className="font-medium text-amber-300">
            {t("vector.assurance_build_assumed_title", {
              count: (buildAssumptions ?? []).length,
            })}
          </p>
          <p className="mt-0.5 text-[11px] text-amber-200/70">
            {t("vector.assurance_build_assumed_hint")}
          </p>
          <ul className="mt-1.5 space-y-1">
            {(buildAssumptions ?? []).map((item, index) => (
              <li
                key={`${index}-${item.slice(0, 40)}`}
                className="text-zinc-300"
              >
                {item}
              </li>
            ))}
          </ul>
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
  overlay,
}: {
  ok?: boolean;
  neutral?: boolean;
  label: string;
  detail?: string;
  /** Где на листе проверка это нашла — миниатюра выреза, по клику крупно. */
  overlay?: { src: string; alt: string };
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
        {detail ? <span className="text-zinc-400"> — {detail}</span> : null}
        {overlay ? (
          <a
            href={overlay.src}
            target="_blank"
            rel="noreferrer"
            className="mt-1 block w-fit"
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={overlay.src}
              alt={overlay.alt}
              loading="lazy"
              className="max-h-32 max-w-full rounded border border-white/10 bg-white"
            />
          </a>
        ) : null}
      </span>
    </li>
  );
}
