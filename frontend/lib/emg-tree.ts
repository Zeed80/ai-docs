import type { EngineeringModelGraphRevision } from "@/lib/engineering-api";

type Graph = EngineeringModelGraphRevision["graph"];
type GraphAssertion = Graph["assertions"][number];

/** One editable param on an operation or feature node — the assertion that
 * carries it, plus everything a person needs to judge and correct it. */
export interface EmgParam {
  assertionId: string;
  predicate: string;
  name: string;
  value: unknown;
  unit: string | null;
  origin: string;
  assurance: string;
  confidence: number;
}

export interface EmgOperationNode {
  id: string;
  sequence: number;
  kind: string;
  kindAssertionId: string | null;
  label: string;
  params: EmgParam[];
  /** Ф2.6c: which descriptive Feature node(s) this operation realizes —
   * empty when the operation has no id-tagged source (a base extrude/
   * revolve built from the whole profile, or a feature added directly in
   * the 3D editor with no reading behind it). */
  featureIds: string[];
}

export interface EmgFeatureNode {
  id: string;
  kind: string | null;
  location: string | null;
  locationAssertionId: string | null;
  params: EmgParam[];
}

export interface EmgTree {
  operations: EmgOperationNode[];
  featuresById: Map<string, EmgFeatureNode>;
}

const OPERATION_PARAM_PREFIX = "operation.param.";
const FEATURE_PARAM_PREFIX = "feature.param.";

function assertionValue(item: GraphAssertion): unknown {
  const value = item.value as { kind?: string; value?: unknown } | undefined;
  return value?.kind === "exact" ? value.value : undefined;
}

/** Human labels for the spec's own kind vocabulary — never invented, just
 * the same strings feature.kind/operation.kind already carry, made
 * readable. Falls back to the raw kind string for anything not listed. */
const KIND_LABELS_RU: Record<string, string> = {
  extrude: "Выдавливание",
  revolve: "Вращение",
  loft: "Лофт",
  sweep: "Протягивание",
  shell: "Оболочка",
  thread: "Резьба",
  hole: "Отверстие",
  boss: "Бобышка",
  pocket: "Карман",
  fillet: "Скругление",
  chamfer: "Фаска",
  groove: "Канавка",
  keyway: "Шпоночный паз",
  rib: "Ребро",
  section_outer: "Наружный профиль",
  section_bore: "Расточка",
  cross_hole: "Поперечное отверстие",
  axial_hole_pattern: "Осевые отверстия",
  circular_hole_pattern: "Круговой массив отверстий",
  hole_pattern: "Массив отверстий",
  slot: "Паз",
};

export function kindLabel(kind: string | null | undefined): string {
  if (!kind) return "—";
  return KIND_LABELS_RU[kind] ?? kind;
}

/** Derive the operation/feature tree from a sealed EMG graph — pure, no
 * network. Mirrors what the kernel boundary itself compiles
 * (compile_build_plan: sort by operation.sequence, active only) so the
 * tree shown in the editor is never out of step with what actually gets
 * built. */
export function buildEmgTree(graph: Graph): EmgTree {
  const nodeType = new Map(graph.nodes.map((node) => [node.id, node.type]));
  const byOperation = new Map<string, GraphAssertion[]>();
  const byFeature = new Map<string, GraphAssertion[]>();
  for (const assertion of graph.assertions) {
    if (assertion.state !== "active") continue;
    const type = nodeType.get(assertion.subject_id);
    if (type === "BuildOperation") {
      const list = byOperation.get(assertion.subject_id) ?? [];
      list.push(assertion);
      byOperation.set(assertion.subject_id, list);
    } else if (type === "Feature") {
      const list = byFeature.get(assertion.subject_id) ?? [];
      list.push(assertion);
      byFeature.set(assertion.subject_id, list);
    }
  }

  const realizesByOperation = new Map<string, string[]>();
  for (const edge of graph.edges) {
    if (edge.type !== "realizes") continue;
    const list = realizesByOperation.get(edge.source_id) ?? [];
    list.push(edge.target_id);
    realizesByOperation.set(edge.source_id, list);
  }

  const featuresById = new Map<string, EmgFeatureNode>();
  for (const [featureId, assertions] of byFeature) {
    const kindAssertion = assertions.find(
      (a) => a.predicate === "feature.kind",
    );
    const locationAssertion = assertions.find(
      (a) => a.predicate === "feature.location",
    );
    const params: EmgParam[] = assertions
      .filter((a) => a.predicate.startsWith(FEATURE_PARAM_PREFIX))
      .map((a) => ({
        assertionId: a.id,
        predicate: a.predicate,
        name: a.predicate.slice(FEATURE_PARAM_PREFIX.length),
        value: assertionValue(a),
        unit: null,
        origin: a.origin,
        assurance: a.assurance,
        confidence: a.confidence,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
    featuresById.set(featureId, {
      id: featureId,
      kind: kindAssertion
        ? ((assertionValue(kindAssertion) as string) ?? null)
        : null,
      location: locationAssertion
        ? ((assertionValue(locationAssertion) as string) ?? null)
        : null,
      locationAssertionId: locationAssertion?.id ?? null,
      params,
    });
  }

  const operations: EmgOperationNode[] = [];
  for (const [operationId, assertions] of byOperation) {
    const kindAssertion = assertions.find(
      (a) => a.predicate === "operation.kind",
    );
    const sequenceAssertion = assertions.find(
      (a) => a.predicate === "operation.sequence",
    );
    const kind = kindAssertion
      ? String(assertionValue(kindAssertion) ?? "?")
      : "?";
    const sequence = sequenceAssertion
      ? Number(assertionValue(sequenceAssertion) ?? 0)
      : Number.MAX_SAFE_INTEGER;
    const params: EmgParam[] = assertions
      .filter((a) => a.predicate.startsWith(OPERATION_PARAM_PREFIX))
      .map((a) => ({
        assertionId: a.id,
        predicate: a.predicate,
        name: a.predicate.slice(OPERATION_PARAM_PREFIX.length),
        value: assertionValue(a),
        unit: null,
        origin: a.origin,
        assurance: a.assurance,
        confidence: a.confidence,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
    operations.push({
      id: operationId,
      sequence,
      kind,
      kindAssertionId: kindAssertion?.id ?? null,
      label: `${kindLabel(kind)}`,
      params,
      featureIds: realizesByOperation.get(operationId) ?? [],
    });
  }
  operations.sort(
    (a, b) => a.sequence - b.sequence || a.id.localeCompare(b.id),
  );

  return { operations, featuresById };
}

// A2's own note format (assign_stable_feature_ids' id, always first):
// "0:outer:4: длина ступени Ø29.5 не указана - построено с предположением
// 29.17 мм...". Decoding it here is inverting a format this codebase
// defines and controls (same contract as cad_emg_compat.feature_spec_path
// on the backend), not a guess about the drawing.
const ASSUMPTION_LEADING_ID = /^(\d+:[\w.]+:\d+): /;

/** Which operation an assumption note is about, if it names a stable
 * Feature id this graph actually has a realizes edge for. */
export function operationForAssumption(
  operations: EmgOperationNode[],
  assumption: string,
): EmgOperationNode | null {
  const match = assumption.match(ASSUMPTION_LEADING_ID);
  if (!match) return null;
  const featureId = `feature:${match[1]}`;
  return operations.find((op) => op.featureIds.includes(featureId)) ?? null;
}

/** Ф2.6c note: cad_solid.py's builders only stamp explicit ParamProvenance
 * on the "headline" read per feature (see e.g. _cut_features' groove,
 * which annotates axial_position_mm but not width_mm/depth_mm) — every
 * OTHER param silently defaults to Assertion.origin="assumed" in the
 * graph projection (spec_feature_tree_as_graph's provenance_origin
 * fallback), even though it was genuinely read off the sheet. Confirmed
 * live: naively flagging every origin="assumed" param lit up almost the
 * whole tree amber for a shaft with only 2 real, reviewable assumptions.
 * solid_3d.assumptions (A2's own curated, human-written list) is the
 * precise signal instead — this derives the SAME operation set the
 * assumptions strip already links to, so the tree's dots and the strip's
 * buttons always agree. */
export function operationsNeedingReview(
  operations: EmgOperationNode[],
  assumptions: string[],
): Set<string> {
  const ids = new Set<string>();
  for (const item of assumptions) {
    if (item.startsWith("critical assertion ")) continue;
    const operation = operationForAssumption(operations, item);
    if (operation) ids.add(operation.id);
  }
  return ids;
}

/** Axis-aligned bounds (mm, the model's own coordinate frame — the same
 * frame the STL/topology.json exports use) of what one build operation
 * actually changed in the B-Rep. */
export interface OperationBounds {
  x_min: number;
  x_max: number;
  y_min: number;
  y_max: number;
  z_min: number;
  z_max: number;
}

/** solid_3d.verification.feature_results (infra/cad-kernel/server.py's
 * _feature_results/_operation_localization) already measures, for nearly
 * every operation kind, the exact bounding box of the geometry a boolean
 * add/cut changed — a before/after B-Rep delta, not a guess. feature_index
 * there is the position in candidate.features, the SAME index
 * spec_feature_tree_as_graph uses for `operation:${index}` node ids
 * (cad_emg_compat.py), so this needs no new backend plumbing: it is a pure
 * re-projection of data the kernel already ships. Absent for an operation
 * that changed nothing (a cosmetic thread) or whose delta could not be
 * localized (kernel_report already flags those in unlocalized_features) —
 * never invented for those. */
export function operationBoundsFromFeatureResults(
  featureResults: unknown,
): Map<string, OperationBounds> {
  const bounds = new Map<string, OperationBounds>();
  if (!Array.isArray(featureResults)) return bounds;
  const keys = ["x_min", "x_max", "y_min", "y_max", "z_min", "z_max"] as const;
  for (const item of featureResults) {
    if (!item || typeof item !== "object") continue;
    const record = item as Record<string, unknown>;
    const index = record.feature_index;
    const box = record.changed_bounds_mm;
    if (typeof index !== "number" || !box || typeof box !== "object") continue;
    const raw = box as Record<string, unknown>;
    const values = keys.map((key) => Number(raw[key]));
    if (values.some((value) => !Number.isFinite(value))) continue;
    const [x_min, x_max, y_min, y_max, z_min, z_max] = values;
    bounds.set(`operation:${index}`, {
      x_min,
      x_max,
      y_min,
      y_max,
      z_min,
      z_max,
    });
  }
  return bounds;
}

/** Whether an assertion (by predicate + subject type) is the kind of thing
 * a human can plausibly correct through the mirror into the compatibility
 * spec — mirrors the server-side allowlist in image_generation.py's
 * _apply_compat_spec_update (product:legacy-spec always; a Feature's own
 * feature.param.<name> or feature.location when it has a spec_path). Kept
 * in sync by hand since the rule is simple and stable; the server is still
 * the authority — a wrong guess here just shows/hides the rebuild
 * checkbox, never causes silent data loss (the server 422s with why). */
export function isLikelyMirrorable(
  subjectId: string,
  predicate: string,
): boolean {
  if (subjectId === "product:legacy-spec") return true;
  if (!subjectId.startsWith("feature:")) return false;
  return (
    predicate.startsWith(FEATURE_PARAM_PREFIX) ||
    predicate === "feature.location"
  );
}
