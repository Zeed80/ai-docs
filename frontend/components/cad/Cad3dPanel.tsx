"use client";

import { useEffect, useMemo, useState } from "react";

import CadModelViewer from "@/components/studio/CadModelViewer";
import {
  AddedCadEdgeFeature,
  AddedCadFeature,
  FeatureParameterOverride,
  FeatureTreeCandidate,
  Generation,
  artifactUrl,
  compileFeatureTreeCandidate,
  getFeatureTreeCandidates,
} from "@/lib/studio-api";

/** The accepted-drawing 3D section: feature-tree candidates, parameter
 * overrides, boss/pocket/fillet/chamfer additions, explicit build and the
 * STL preview. Owns ALL of its state — the sketch editor only tells it the
 * generation and current revision; a revision change naturally invalidates
 * the built artifact (cadReadyRevision !== revision). */
export default function Cad3dPanel({
  gen,
  revision,
  onChanged,
  onError,
  t,
}: {
  gen: Generation;
  revision: number;
  onChanged: () => void;
  onError: (message: string) => void;
  t: (k: string, v?: Record<string, string | number>) => string;
}) {
  const [featureCandidates, setFeatureCandidates] = useState<
    FeatureTreeCandidate[]
  >([]);
  const [selectedCandidateIndex, setSelectedCandidateIndex] = useState(
    typeof gen.params?.cad_candidate_index === "number"
      ? (gen.params.cad_candidate_index as number)
      : 0,
  );
  const [cadFeatureOverrides, setCadFeatureOverrides] = useState<
    FeatureParameterOverride[]
  >(() =>
    Array.isArray(gen.params?.cad_feature_overrides)
      ? (gen.params.cad_feature_overrides as FeatureParameterOverride[])
      : [],
  );
  const [cadParametersDirty, setCadParametersDirty] = useState(false);
  const [cadAddedFeatures, setCadAddedFeatures] = useState<AddedCadFeature[]>(
    () =>
      Array.isArray(gen.params?.cad_added_features)
        ? (
            gen.params.cad_added_features as (
              AddedCadFeature | AddedCadEdgeFeature
            )[]
          ).filter(
            (feature): feature is AddedCadFeature =>
              feature.kind === "boss" || feature.kind === "pocket",
          )
        : [],
  );
  const [cadEdgeFeatures, setCadEdgeFeatures] = useState<AddedCadEdgeFeature[]>(
    () =>
      Array.isArray(gen.params?.cad_added_features)
        ? (
            gen.params.cad_added_features as (
              AddedCadFeature | AddedCadEdgeFeature
            )[]
          ).filter(
            (feature): feature is AddedCadEdgeFeature =>
              feature.kind === "fillet" || feature.kind === "chamfer",
          )
        : [],
  );
  const [cadBuiltCandidateIndex, setCadBuiltCandidateIndex] = useState<
    number | null
  >(
    typeof gen.params?.cad_candidate_index === "number"
      ? (gen.params.cad_candidate_index as number)
      : null,
  );
  const [cadCandidatesLoading, setCadCandidatesLoading] = useState(false);
  const [cadBuilding, setCadBuilding] = useState(false);
  const [cadPreviewVersion, setCadPreviewVersion] = useState(0);
  const [cadReadyRevision, setCadReadyRevision] = useState<number | null>(
    typeof gen.params?.cad_artifact_revision === "number"
      ? (gen.params.cad_artifact_revision as number)
      : null,
  );

  useEffect(() => {
    let cancelled = false;
    if (!gen.accepted) {
      setFeatureCandidates([]);
      return;
    }
    setCadCandidatesLoading(true);
    void getFeatureTreeCandidates(gen.id)
      .then((items) => {
        if (!cancelled) {
          setFeatureCandidates(items);
          setSelectedCandidateIndex((current) =>
            Math.min(current, Math.max(0, items.length - 1)),
          );
        }
      })
      .catch((e) => !cancelled && onError(String((e as Error).message || e)))
      .finally(() => !cancelled && setCadCandidatesLoading(false));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gen.accepted, gen.id, revision]);

  const selectedCandidate = featureCandidates[selectedCandidateIndex] ?? null;
  const cadReport = (gen.params?.cad_report ?? null) as {
    volume_mm3?: number;
    solid_count?: number;
    bounds_mm?: { x?: number; y?: number; z?: number };
    warnings?: string[];
    edges?: Array<{
      key: string;
      index: number;
      curve: string;
      length_mm: number;
      vertices: Array<{ x: number; y: number; z: number }>;
    }>;
  } | null;

  const unresolvedCadAssumptions = useMemo(() => {
    if (!selectedCandidate) return [];
    const resolvedMarkers = new Set<string>();
    selectedCandidate.features.forEach((feature, index) => {
      const override = cadFeatureOverrides.find(
        (item) => item.feature_index === index,
      );
      if (feature.kind === "extrude" && override?.depth_mm != null) {
        resolvedMarkers.add("extrude-depth");
      }
      if (feature.kind === "hole" && override?.through != null) {
        resolvedMarkers.add(
          `hole-${Number(feature.params.diameter_mm ?? 0).toFixed(6)}`,
        );
      }
    });
    return selectedCandidate.missing_data.filter((item) => {
      if (
        resolvedMarkers.has("extrude-depth") &&
        (item.includes("бокового вида") ||
          item.includes("глубина выдавливания"))
      ) {
        return false;
      }
      for (const marker of resolvedMarkers) {
        if (!marker.startsWith("hole-")) continue;
        const diameter = Number(marker.slice(5));
        const match = item.match(/глубина отверстия ([\d.,]+)мм/);
        if (
          match &&
          Math.abs(Number(match[1].replace(",", ".")) - diameter) < 1e-6
        )
          return false;
      }
      return true;
    });
  }, [selectedCandidate, cadFeatureOverrides]);

  const cadArtifactCurrent =
    cadReadyRevision === revision &&
    cadBuiltCandidateIndex === selectedCandidateIndex &&
    !cadParametersDirty;

  function featureOverride(
    index: number,
  ): FeatureParameterOverride | undefined {
    return cadFeatureOverrides.find((item) => item.feature_index === index);
  }

  function updateFeatureOverride(
    index: number,
    patch: Omit<FeatureParameterOverride, "feature_index">,
  ) {
    setCadFeatureOverrides((current) => {
      const existing = current.find((item) => item.feature_index === index) ?? {
        feature_index: index,
      };
      return [
        ...current.filter((item) => item.feature_index !== index),
        { ...existing, ...patch },
      ].sort((a, b) => a.feature_index - b.feature_index);
    });
    setCadParametersDirty(true);
  }

  function addCadFeature(kind: "boss" | "pocket") {
    if (!selectedCandidate) return;
    const base = selectedCandidate.features.find(
      (feature) => feature.kind === "extrude",
    );
    const width = Number(base?.params.width_mm ?? 100);
    const height = Number(base?.params.height_mm ?? 100);
    const depth =
      featureOverride(selectedCandidate.features.indexOf(base!))?.depth_mm ??
      Number(base?.params.depth_mm ?? 10);
    setCadAddedFeatures((current) => [
      ...current,
      {
        kind,
        profile: "circle",
        center_x_mm: width / 2,
        center_y_mm: height / 2,
        depth_mm: Math.max(
          0.1,
          Math.min(depth / 4, kind === "pocket" ? depth - 0.1 : depth),
        ),
        diameter_mm: Math.max(0.1, Math.min(width, height) / 4),
      },
    ]);
    setCadParametersDirty(true);
  }

  function updateAddedCadFeature(
    index: number,
    patch: Partial<AddedCadFeature>,
  ) {
    setCadAddedFeatures((current) =>
      current.map((feature, itemIndex) => {
        if (itemIndex !== index) return feature;
        const updated = { ...feature, ...patch };
        if (patch.profile === "circle") {
          delete updated.width_mm;
          delete updated.height_mm;
          updated.diameter_mm ??= 10;
        } else if (patch.profile === "rectangle") {
          delete updated.diameter_mm;
          updated.width_mm ??= 10;
          updated.height_mm ??= 10;
        }
        return updated;
      }),
    );
    setCadParametersDirty(true);
  }

  function removeAddedCadFeature(index: number) {
    setCadAddedFeatures((current) =>
      current.filter((_, itemIndex) => itemIndex !== index),
    );
    setCadParametersDirty(true);
  }

  function addCadEdgeFeature(kind: "fillet" | "chamfer") {
    const edge = cadReport?.edges?.[0];
    if (!edge) return;
    setCadEdgeFeatures((current) => [
      ...current,
      { kind, edge_key: edge.key, size_mm: 1 },
    ]);
    setCadParametersDirty(true);
  }

  function updateCadEdgeFeature(
    index: number,
    patch: Partial<AddedCadEdgeFeature>,
  ) {
    setCadEdgeFeatures((current) =>
      current.map((feature, itemIndex) =>
        itemIndex === index ? { ...feature, ...patch } : feature,
      ),
    );
    setCadParametersDirty(true);
  }

  function removeCadEdgeFeature(index: number) {
    setCadEdgeFeatures((current) =>
      current.filter((_, itemIndex) => itemIndex !== index),
    );
    setCadParametersDirty(true);
  }

  async function buildCadModel() {
    if (!selectedCandidate) return;
    const hasAssumptions = unresolvedCadAssumptions.length > 0;
    if (
      hasAssumptions &&
      !window.confirm(
        t("vector.cad_confirm_assumptions", {
          n: selectedCandidate.missing_data.length,
        }),
      )
    ) {
      return;
    }
    setCadBuilding(true);
    try {
      await compileFeatureTreeCandidate(
        gen.id,
        selectedCandidateIndex,
        hasAssumptions,
        cadFeatureOverrides,
        [...cadAddedFeatures, ...cadEdgeFeatures],
      );
      setCadReadyRevision(revision);
      setCadBuiltCandidateIndex(selectedCandidateIndex);
      setCadParametersDirty(false);
      setCadPreviewVersion((value) => value + 1);
      onChanged();
    } catch (e) {
      onError(String((e as Error).message || e));
    } finally {
      setCadBuilding(false);
    }
  }

  if (!gen.accepted) return null;

  return (
    <section className="space-y-3 border-t border-white/10 pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-zinc-100">
            {t("vector.cad_title")}
          </div>
          <div className="text-[11px] text-zinc-500">
            {t("vector.cad_revision", { revision })}
          </div>
        </div>
        {featureCandidates.length > 0 && (
          <div className="flex min-w-0 items-center gap-2">
            <select
              value={selectedCandidateIndex}
              onChange={(event) => {
                setSelectedCandidateIndex(Number(event.target.value));
                setCadFeatureOverrides([]);
                setCadAddedFeatures([]);
                setCadEdgeFeatures([]);
                setCadParametersDirty(true);
              }}
              disabled={cadBuilding}
              aria-label={t("vector.cad_candidate")}
              className="min-w-0 max-w-[420px] rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
            >
              {featureCandidates.map((candidate, index) => (
                <option key={`${candidate.label}-${index}`} value={index}>
                  {index + 1}. {candidate.label} (
                  {Math.round(candidate.score * 100)}%)
                </option>
              ))}
            </select>
            <button
              type="button"
              disabled={cadBuilding || !selectedCandidate}
              onClick={() => void buildCadModel()}
              className="shrink-0 rounded bg-sky-600 px-3 py-1.5 text-xs text-white hover:bg-sky-500 disabled:opacity-50"
            >
              {cadBuilding ? t("vector.cad_building") : t("vector.cad_build")}
            </button>
          </div>
        )}
      </div>

      {cadCandidatesLoading && (
        <div className="text-xs text-zinc-500">
          {t("vector.cad_candidates_loading")}
        </div>
      )}
      {!cadCandidatesLoading && featureCandidates.length === 0 && (
        <div className="text-xs text-amber-300">
          {t("vector.cad_no_candidates")}
        </div>
      )}
      {selectedCandidate && unresolvedCadAssumptions.length > 0 && (
        <div className="border-l-2 border-amber-400/60 pl-3">
          <div className="text-[11px] font-medium text-amber-300">
            {t("vector.cad_missing_title", {
              n: unresolvedCadAssumptions.length,
            })}
          </div>
          <ul className="mt-1 space-y-0.5 text-[11px] text-zinc-400">
            {unresolvedCadAssumptions.map((item, index) => (
              <li key={`${item}-${index}`}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {selectedCandidate && (
        <div
          data-testid="cad-feature-tree"
          className="border-y border-white/10 py-2"
        >
          <div className="mb-2 text-[11px] font-medium uppercase text-zinc-400">
            {t("vector.cad_tree_title")}
          </div>
          <div className="space-y-2">
            {selectedCandidate.features.map((feature, index) => {
              const override = featureOverride(index);
              if (feature.kind === "extrude") {
                const depth =
                  override?.depth_mm ?? Number(feature.params.depth_mm ?? 0);
                return (
                  <div
                    key={`feature-${index}`}
                    className="grid grid-cols-[minmax(110px,1fr)_120px] items-center gap-3 text-xs"
                  >
                    <div className="min-w-0">
                      <div className="text-zinc-200">
                        {index + 1}. {t("vector.cad_extrude")}
                      </div>
                      <div className="truncate text-[11px] text-zinc-500">
                        {Number(feature.params.width_mm ?? 0).toFixed(2)} ×{" "}
                        {Number(feature.params.height_mm ?? 0).toFixed(2)} mm
                      </div>
                    </div>
                    <label className="grid grid-cols-[1fr_auto] items-center gap-1 text-[11px] text-zinc-400">
                      <input
                        type="number"
                        min="0.01"
                        step="0.1"
                        value={depth}
                        aria-label={t("vector.cad_depth")}
                        onChange={(event) =>
                          updateFeatureOverride(index, {
                            depth_mm: Number(event.target.value),
                          })
                        }
                        className="min-w-0 rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                      />
                      mm
                    </label>
                  </div>
                );
              }
              if (feature.kind === "hole") {
                const hasThroughOverride =
                  override &&
                  Object.prototype.hasOwnProperty.call(override, "through");
                const through = hasThroughOverride
                  ? override?.through
                  : (feature.params.through as boolean | null | undefined);
                const base = selectedCandidate.features.find(
                  (item) => item.kind === "extrude",
                );
                const baseIndex = selectedCandidate.features.findIndex(
                  (item) => item.kind === "extrude",
                );
                const baseDepth =
                  featureOverride(baseIndex)?.depth_mm ??
                  Number(base?.params.depth_mm ?? 10);
                const blindDepth =
                  override?.depth_mm ??
                  Math.max(0.1, Math.min(baseDepth / 2, baseDepth - 0.1));
                return (
                  <div
                    key={`feature-${index}`}
                    className="grid grid-cols-[minmax(110px,1fr)_minmax(150px,220px)] items-center gap-3 text-xs"
                  >
                    <div className="min-w-0">
                      <div className="text-zinc-200">
                        {index + 1}. {t("vector.cad_hole")} ⌀
                        {Number(feature.params.diameter_mm ?? 0).toFixed(2)} mm
                      </div>
                      <div className="truncate text-[11px] text-zinc-500">
                        {t("vector.cad_hole_position", {
                          x: Number(feature.params.center_x_mm ?? 0).toFixed(2),
                          y: Number(feature.params.center_y_mm ?? 0).toFixed(2),
                        })}
                      </div>
                    </div>
                    <div className="flex min-w-0 items-center gap-2">
                      <select
                        value={
                          through === true
                            ? "through"
                            : through === false
                              ? "blind"
                              : "unknown"
                        }
                        aria-label={t("vector.cad_hole_type")}
                        onChange={(event) => {
                          if (event.target.value === "through")
                            updateFeatureOverride(index, {
                              through: true,
                              depth_mm: undefined,
                            });
                          else if (event.target.value === "blind")
                            updateFeatureOverride(index, {
                              through: false,
                              depth_mm: blindDepth,
                            });
                          else
                            updateFeatureOverride(index, {
                              through: null,
                              depth_mm: undefined,
                            });
                        }}
                        className="min-w-0 flex-1 rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
                      >
                        <option value="through">
                          {t("vector.cad_through")}
                        </option>
                        <option value="blind">{t("vector.cad_blind")}</option>
                        <option value="unknown">
                          {t("vector.cad_unknown")}
                        </option>
                      </select>
                      {through === false && (
                        <label className="flex w-[92px] items-center gap-1 text-[11px] text-zinc-400">
                          <input
                            type="number"
                            min="0.01"
                            max={Math.max(0.01, baseDepth - 0.01)}
                            step="0.1"
                            value={blindDepth}
                            aria-label={t("vector.cad_blind_depth")}
                            onChange={(event) =>
                              updateFeatureOverride(index, {
                                through: false,
                                depth_mm: Number(event.target.value),
                              })
                            }
                            className="min-w-0 rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                          />
                          mm
                        </label>
                      )}
                    </div>
                  </div>
                );
              }
              return null;
            })}
            {cadAddedFeatures.map((feature, index) => (
              <div
                key={`added-feature-${index}`}
                className="border-t border-white/10 pt-2"
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-xs text-zinc-200">
                    {selectedCandidate.features.length + index + 1}.{" "}
                    {t(
                      feature.kind === "boss"
                        ? "vector.cad_boss"
                        : "vector.cad_pocket",
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => removeAddedCadFeature(index)}
                    aria-label={t("vector.cad_remove_feature")}
                    title={t("vector.cad_remove_feature")}
                    className="grid h-7 w-7 place-items-center text-lg text-zinc-500 hover:text-red-400"
                  >
                    ×
                  </button>
                </div>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  <label className="text-[10px] text-zinc-500">
                    {t("vector.cad_profile")}
                    <select
                      value={feature.profile}
                      onChange={(event) =>
                        updateAddedCadFeature(index, {
                          profile: event.target
                            .value as AddedCadFeature["profile"],
                        })
                      }
                      className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
                    >
                      <option value="circle">
                        {t("vector.cad_profile_circle")}
                      </option>
                      <option value="rectangle">
                        {t("vector.cad_profile_rectangle")}
                      </option>
                    </select>
                  </label>
                  {(
                    [
                      ["center_x_mm", "vector.cad_center_x"],
                      ["center_y_mm", "vector.cad_center_y"],
                      ["depth_mm", "vector.cad_operation_depth"],
                    ] as const
                  ).map(([field, label]) => (
                    <label key={field} className="text-[10px] text-zinc-500">
                      {t(label)}
                      <input
                        type="number"
                        min={field === "depth_mm" ? "0.01" : "0"}
                        step="0.1"
                        value={feature[field]}
                        onChange={(event) =>
                          updateAddedCadFeature(index, {
                            [field]: Number(event.target.value),
                          })
                        }
                        className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                      />
                    </label>
                  ))}
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 sm:max-w-[50%]">
                  {feature.profile === "circle" ? (
                    <label className="text-[10px] text-zinc-500">
                      {t("vector.cad_diameter")}
                      <input
                        type="number"
                        min="0.01"
                        step="0.1"
                        value={feature.diameter_mm ?? 0}
                        onChange={(event) =>
                          updateAddedCadFeature(index, {
                            diameter_mm: Number(event.target.value),
                          })
                        }
                        className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                      />
                    </label>
                  ) : (
                    <>
                      <label className="text-[10px] text-zinc-500">
                        {t("vector.cad_width")}
                        <input
                          type="number"
                          min="0.01"
                          step="0.1"
                          value={feature.width_mm ?? 0}
                          onChange={(event) =>
                            updateAddedCadFeature(index, {
                              width_mm: Number(event.target.value),
                            })
                          }
                          className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                        />
                      </label>
                      <label className="text-[10px] text-zinc-500">
                        {t("vector.cad_height")}
                        <input
                          type="number"
                          min="0.01"
                          step="0.1"
                          value={feature.height_mm ?? 0}
                          onChange={(event) =>
                            updateAddedCadFeature(index, {
                              height_mm: Number(event.target.value),
                            })
                          }
                          className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                        />
                      </label>
                    </>
                  )}
                </div>
              </div>
            ))}
            {cadEdgeFeatures.map((feature, index) => (
              <div
                key={`edge-feature-${index}`}
                className="border-t border-white/10 pt-2"
              >
                <div className="mb-2 flex items-center justify-between gap-2">
                  <div className="text-xs text-zinc-200">
                    {selectedCandidate.features.length +
                      cadAddedFeatures.length +
                      index +
                      1}
                    .{" "}
                    {t(
                      feature.kind === "fillet"
                        ? "vector.cad_fillet"
                        : "vector.cad_chamfer",
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => removeCadEdgeFeature(index)}
                    aria-label={t("vector.cad_remove_feature")}
                    title={t("vector.cad_remove_feature")}
                    className="grid h-7 w-7 place-items-center text-lg text-zinc-500 hover:text-red-400"
                  >
                    ×
                  </button>
                </div>
                <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-2">
                  <label className="text-[10px] text-zinc-500">
                    {t("vector.cad_edge")}
                    <select
                      value={feature.edge_key}
                      onChange={(event) =>
                        updateCadEdgeFeature(index, {
                          edge_key: event.target.value,
                        })
                      }
                      className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-xs text-zinc-200"
                    >
                      {(cadReport?.edges ?? []).map((edge) => (
                        <option key={edge.key} value={edge.key}>
                          #{edge.index} · {edge.curve} ·{" "}
                          {edge.length_mm.toFixed(2)} mm
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="text-[10px] text-zinc-500">
                    {t(
                      feature.kind === "fillet"
                        ? "vector.cad_radius"
                        : "vector.cad_chamfer_size",
                    )}
                    <input
                      type="number"
                      min="0.01"
                      step="0.1"
                      value={feature.size_mm}
                      onChange={(event) =>
                        updateCadEdgeFeature(index, {
                          size_mm: Number(event.target.value),
                        })
                      }
                      className="mt-0.5 w-full rounded border border-white/10 bg-zinc-900 px-2 py-1 text-right text-xs text-zinc-100"
                    />
                  </label>
                </div>
              </div>
            ))}
            <div className="flex flex-wrap gap-2 border-t border-white/10 pt-2">
              <button
                type="button"
                onClick={() => addCadFeature("boss")}
                className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
              >
                + {t("vector.cad_add_boss")}
              </button>
              <button
                type="button"
                onClick={() => addCadFeature("pocket")}
                className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
              >
                + {t("vector.cad_add_pocket")}
              </button>
              {(cadReport?.edges?.length ?? 0) > 0 && (
                <>
                  <button
                    type="button"
                    onClick={() => addCadEdgeFeature("fillet")}
                    className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
                  >
                    + {t("vector.cad_add_fillet")}
                  </button>
                  <button
                    type="button"
                    onClick={() => addCadEdgeFeature("chamfer")}
                    className="rounded border border-white/10 px-2 py-1 text-xs text-zinc-300 hover:bg-white/5"
                  >
                    + {t("vector.cad_add_chamfer")}
                  </button>
                </>
              )}
            </div>
          </div>
          {cadParametersDirty && (
            <div className="mt-2 text-[11px] text-amber-300">
              {t("vector.cad_rebuild_required")}
            </div>
          )}
        </div>
      )}

      {cadArtifactCurrent && (
        <>
          <CadModelViewer
            url={`${artifactUrl(gen.id, "stl")}&v=${cadPreviewVersion}`}
            loadingLabel={t("vector.cad_preview_loading")}
            errorLabel={t("vector.cad_preview_error")}
          />
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-zinc-400">
            {cadReport?.bounds_mm && (
              <span>
                {t("vector.cad_bounds", {
                  x: Number(cadReport.bounds_mm.x ?? 0).toFixed(2),
                  y: Number(cadReport.bounds_mm.y ?? 0).toFixed(2),
                  z: Number(cadReport.bounds_mm.z ?? 0).toFixed(2),
                })}
              </span>
            )}
            {typeof cadReport?.volume_mm3 === "number" && (
              <span>
                {t("vector.cad_volume", {
                  value: cadReport.volume_mm3.toFixed(2),
                })}
              </span>
            )}
            <a
              href={artifactUrl(gen.id, "step")}
              download={`studio-${gen.id}.step`}
              className="text-sky-400 hover:text-sky-300"
            >
              STEP
            </a>
            <a
              href={artifactUrl(gen.id, "fcstd")}
              download={`studio-${gen.id}.FCStd`}
              className="text-sky-400 hover:text-sky-300"
            >
              FCStd
            </a>
          </div>
          {(cadReport?.warnings?.length ?? 0) > 0 && (
            <div className="text-[11px] text-amber-300">
              {cadReport?.warnings?.join("; ")}
            </div>
          )}
        </>
      )}
    </section>
  );
}
