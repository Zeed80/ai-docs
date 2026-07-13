"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const BASE = `${API}/api/image-gen`;

// "eskd" = text→image ЕСКД-styled diffusion (alternative to the deterministic
// techDraw() vector render, which is not a ComfyUI operation).
// "vectorize" = deterministic scan→CAD-IR→DXF digitization (no diffusion).
export type Operation =
  "generate" | "edit" | "inpaint" | "cleanup" | "eskd" | "vectorize";
export type TechDrawView = "front" | "isometric" | "section" | "half_section";
export type GenStatus = "queued" | "running" | "cancelled" | "done" | "failed";
export type StudioJobStatus =
  | "queued"
  | "waiting_resource"
  | "running"
  | "cancel_requested"
  | "cancelled"
  | "done"
  | "failed";
export type StudioJobKind = "image_generation" | "lora_training";

export interface GenProgress {
  value: number | null;
  max: number | null;
  pct: number;
  node: string | null;
  ts: number;
}

export interface Generation {
  id: string;
  job_id?: string;
  operation: Operation;
  status: GenStatus;
  progress: GenProgress | null;
  prompt: string | null;
  negative_prompt: string | null;
  params: Record<string, unknown>;
  source_image_paths: string[];
  mask_path: string | null;
  has_result: boolean;
  error: string | null;
  parent_id: string | null;
  accepted: boolean;
  accepted_by?: string | null;
  accepted_at?: string | null;
  accepted_revision?: number | null;
  quality_rating?: number | null;
  issue_tags?: string[];
  review_notes?: string | null;
  workflow_id: string | null;
  created_at: string | null;
  source_document_id: string | null;
  case_id: string | null;
}

export interface StudioJob {
  id: string;
  kind: StudioJobKind;
  status: StudioJobStatus;
  resource: string;
  title: string | null;
  priority: number;
  position: number | null;
  eta_seconds: number | null;
  owner_sub: string | null;
  created_at: string | null;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  cancel_requested_at: string | null;
  generation_id: string | null;
  lora_run_id: string | null;
  linked_status: string | null;
  progress: GenProgress | Record<string, unknown> | null;
  error: string | null;
  can_cancel: boolean;
  can_retry?: boolean;
  meta: Record<string, unknown>;
}

export interface StudioQueueStats {
  control: {
    paused: boolean;
    drain: boolean;
    reason: string | null;
    updated_at: string | null;
    updated_by: string | null;
  };
  limits: {
    global_active: number;
    per_user_active: number;
    operator_active: number;
  };
  totals: Record<string, number>;
  active: number;
  by_resource: Record<string, Record<string, number>>;
  by_kind: Record<string, Record<string, number>>;
  avg_wait_seconds_24h: number | null;
  avg_runtime_seconds_24h: number | null;
}

export interface Workflow {
  id: string;
  key: string;
  title: string;
  description: string | null;
  category: string;
  operation: string;
  graph: Record<string, unknown>;
  inject_map: Record<string, unknown>;
  params_schema: Record<string, unknown>;
  enabled: boolean;
  is_builtin: boolean;
}

export interface GenerateInput {
  operation: Operation;
  prompt?: string;
  negative_prompt?: string;
  workflow_id?: string;
  params?: Record<string, unknown>;
  source_image_paths?: string[];
  source_document_ids?: string[];
  mask_path?: string;
  /** Attach the result to a document/case for traceability (not an image source). */
  source_document_id?: string;
  case_id?: string;
}

/** Backend errors carry either a plain string `detail` (legacy) or a typed
 * `{code, message}` object (e.g. IrPatchErrorCode — see cad_validate.py /
 * image_generation.py PATCH /ir). Extract a displayable string either way —
 * `body?.detail as string` alone is a compile-time-only cast that silently
 * stringifies an object to "[object Object]" at runtime. */
function _errorMessage(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail) return detail;
  if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    if (typeof obj.message === "string" && obj.message) return obj.message;
  }
  return fallback;
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = _errorMessage(body?.detail, detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export async function uploadSource(
  file: File,
  kind: "source" | "mask" = "source",
): Promise<string> {
  const form = new FormData();
  form.append("file", file);
  form.append("kind", kind);
  const res = await mutFetch(`${BASE}/upload-source`, {
    method: "POST",
    body: form,
  });
  const body = await jsonOrThrow<{ path: string }>(res);
  return body.path;
}

export async function importDxf(
  file: File,
  title?: string,
): Promise<Generation> {
  const form = new FormData();
  form.append("file", file);
  if (title) form.append("title", title);
  const res = await mutFetch(`${BASE}/import-dxf`, {
    method: "POST",
    body: form,
  });
  return jsonOrThrow<Generation>(res);
}

export async function generate(input: GenerateInput): Promise<Generation> {
  const res = await mutFetch(`${BASE}/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<Generation>(res);
}

export async function listGenerations(): Promise<Generation[]> {
  const res = await apiFetch(`${BASE}`);
  const body = await jsonOrThrow<{ items: Generation[] }>(res);
  return body.items;
}

export async function getGeneration(id: string): Promise<Generation> {
  const res = await apiFetch(`${BASE}/${id}`);
  return jsonOrThrow<Generation>(res);
}

export async function acceptGeneration(id: string): Promise<Generation> {
  const res = await mutFetch(`${BASE}/${id}/accept`, { method: "POST" });
  return jsonOrThrow<Generation>(res);
}

export async function updateGenerationMeta(
  id: string,
  meta: { title?: string; project?: string; object?: string },
): Promise<Generation> {
  const res = await mutFetch(`${BASE}/${id}/meta`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(meta),
  });
  return jsonOrThrow<Generation>(res);
}

export async function iterateGeneration(
  id: string,
  input: GenerateInput,
): Promise<Generation> {
  const res = await mutFetch(`${BASE}/${id}/iterate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<Generation>(res);
}

export async function deleteGeneration(id: string): Promise<void> {
  const res = await mutFetch(`${BASE}/${id}`, { method: "DELETE" });
  await jsonOrThrow(res);
}

export async function bulkDeleteGenerations(ids: string[]): Promise<void> {
  const res = await mutFetch(`${BASE}/bulk-delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  await jsonOrThrow(res);
}

export async function clearFailedGenerations(): Promise<{ deleted: number }> {
  const res = await mutFetch(`${BASE}/clear-failed`, { method: "POST" });
  return jsonOrThrow<{ deleted: number }>(res);
}

export async function listStudioQueue(params?: {
  status?: string;
  kind?: StudioJobKind | "";
  mine?: boolean;
  limit?: number;
}): Promise<StudioJob[]> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.kind) qs.set("kind", params.kind);
  if (params?.mine) qs.set("mine", "true");
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await apiFetch(`${API}/api/studio/queue${suffix}`);
  const body = await jsonOrThrow<{ items: StudioJob[] }>(res);
  return body.items;
}

export async function getStudioQueueStats(): Promise<StudioQueueStats> {
  const res = await apiFetch(`${API}/api/studio/queue/stats`);
  return jsonOrThrow<StudioQueueStats>(res);
}

export async function setStudioQueueControl(input: {
  paused?: boolean;
  drain?: boolean;
  reason?: string | null;
}): Promise<StudioQueueStats["control"]> {
  const res = await mutFetch(`${API}/api/studio/queue/control`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<StudioQueueStats["control"]>(res);
}

export async function getStudioJob(id: string): Promise<StudioJob> {
  const res = await apiFetch(`${API}/api/studio/jobs/${id}`);
  return jsonOrThrow<StudioJob>(res);
}

export async function cancelStudioJob(id: string): Promise<StudioJob> {
  const res = await mutFetch(`${API}/api/studio/queue/${id}/cancel`, {
    method: "POST",
  });
  return jsonOrThrow<StudioJob>(res);
}

export async function retryStudioJob(id: string): Promise<StudioJob> {
  const res = await mutFetch(`${API}/api/studio/queue/${id}/retry`, {
    method: "POST",
  });
  return jsonOrThrow<StudioJob>(res);
}

export async function setStudioJobPriority(
  id: string,
  priority: number,
): Promise<StudioJob> {
  const res = await mutFetch(`${API}/api/studio/queue/${id}/priority`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ priority }),
  });
  return jsonOrThrow<StudioJob>(res);
}

export async function bulkCancelStudioQueue(
  input: {
    resource?: string;
    owner_sub?: string;
  } = {},
): Promise<{ cancelled: number }> {
  const res = await mutFetch(`${API}/api/studio/queue/bulk-cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<{ cancelled: number }>(res);
}

export async function techDraw(
  description: string,
  view: TechDrawView = "front",
  link?: { source_document_id?: string; case_id?: string },
): Promise<Generation> {
  const res = await mutFetch(`${BASE}/techdraw`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description, view, ...link }),
  });
  return jsonOrThrow<Generation>(res);
}

export async function promptHelp(
  description: string,
  operation: Operation,
): Promise<{ prompt: string; negative_prompt: string }> {
  const res = await mutFetch(`${BASE}/prompt-help`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description, operation }),
  });
  return jsonOrThrow(res);
}

export async function listWorkflows(): Promise<Workflow[]> {
  const res = await apiFetch(`${BASE}/workflows/list`);
  const body = await jsonOrThrow<{ items: Workflow[] }>(res);
  return body.items;
}

export async function createWorkflow(wf: Partial<Workflow>): Promise<Workflow> {
  const res = await mutFetch(`${BASE}/workflows`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(wf),
  });
  return jsonOrThrow<Workflow>(res);
}

export async function duplicateWorkflow(id: string): Promise<Workflow> {
  const res = await mutFetch(`${BASE}/workflows/${id}/duplicate`, {
    method: "POST",
  });
  return jsonOrThrow<Workflow>(res);
}

export async function patchWorkflow(
  id: string,
  patch: Partial<Workflow>,
): Promise<Workflow> {
  const res = await mutFetch(`${BASE}/workflows/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return jsonOrThrow<Workflow>(res);
}

export async function deleteWorkflow(id: string): Promise<void> {
  const res = await mutFetch(`${BASE}/workflows/${id}`, { method: "DELETE" });
  await jsonOrThrow(res);
}

/** Saves this workflow's graph into ComfyUI's own userdata/workflows folder
 * so it appears in the embedded ComfyUI UI's own Workflow browser. */
export async function pushWorkflowToComfyUI(
  id: string,
): Promise<{ ok: boolean; filename: string }> {
  const res = await mutFetch(`${BASE}/workflows/${id}/push-to-comfyui`, {
    method: "POST",
  });
  return jsonOrThrow(res);
}

/** URL for the embedded live ComfyUI UI (authenticated reverse proxy — same-
 * origin as the API, so the existing session cookie carries through
 * automatically; must keep the trailing slash — ComfyUI's own frontend
 * derives its API base path from `location.pathname`). */
export const COMFYUI_PROXY_URL = `${API}/api/comfyui-proxy/`;

/** URL for the result/thumbnail/source image (served by the backend, auth via cookie). */
export function resultUrl(id: string, thumb = false): string {
  return `${BASE}/${id}/result${thumb ? "?thumb=true" : ""}`;
}

export function sourceUrl(
  id: string,
  index = 0,
  variant: "original" | "normalized" = "original",
): string {
  return `${BASE}/${id}/source?index=${index}&variant=${variant}`;
}

export type ArtifactKind =
  "dxf" | "dwg" | "svg" | "ir" | "step" | "iges" | "fcstd" | "stl" | "pdf";

export function artifactUrl(id: string, kind: ArtifactKind): string {
  return `${BASE}/${id}/artifact?kind=${encodeURIComponent(kind)}`;
}

// ── CAD IR (vectorize / manual drafting) ─────────────────────────────────────

export type IrLineClass =
  "contour" | "axis" | "dim" | "hatch" | "hidden" | "thin";
export type IrWidthClass = "main" | "thin";

export interface IrPoint {
  x: number;
  y: number;
}

export type IrAssurance =
  | "observed"
  | "inferred"
  | "constraint_validated"
  | "calculation_validated"
  | "human_approved";

export interface IrSourceRegion {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface IrAlternative {
  value?: string | null;
  entity?: Record<string, unknown> | null;
  p: number;
}

export interface IrEntity {
  id: string;
  type:
    | "segment"
    | "arc"
    | "circle"
    | "polyline"
    | "text"
    | "dimension"
    | "hatch"
    | "annotation";
  line_class: IrLineClass;
  width_class: IrWidthClass;
  confidence: number;
  origin: "neural" | "vlm" | "cv" | "human" | "spec";
  assurance: IrAssurance;
  source_region?: IrSourceRegion | null;
  construction?: boolean;
  alternatives?: IrAlternative[];
  evidence?: string[];
  p1?: IrPoint;
  p2?: IrPoint;
  center?: IrPoint;
  radius?: number;
  start_angle?: number;
  end_angle?: number;
  points?: IrPoint[];
  boundary?: IrPoint[];
  holes?: IrPoint[][];
  closed?: boolean;
  position?: IrPoint;
  text?: string;
  height?: number;
  rotation?: number;
  kind?: string;
  value_mm?: number | null;
  tolerance?: string | null;
  pattern?: string;
  // annotation (C4)
  value?: string | null;
  symbol?: string | null;
  datum_refs?: string[];
  leader?: IrPoint | null;
}

export interface IrValidationIssue {
  code: string;
  severity: "error" | "warn" | "info";
  entity_ids: string[];
  message_ru: string;
  level: number;
  norm_ref?: string | null;
  rule_id?: string | null;
  fix_hint?: string | null;
}

export interface IrReviewItem {
  entity_id: string;
  reason: string;
  resolved: boolean;
}

export interface CadIr {
  schema_version: number;
  units: string;
  scale: number | null;
  scale_source: "manual" | "calibration" | "dpi" | "sheet_format" | null;
  source: {
    generation_id: string | null;
    image_width: number;
    image_height: number;
    kind: string;
  };
  sheet: {
    format: string | null;
    width_mm: number | null;
    height_mm: number | null;
    frame: boolean;
    title_block: Record<string, unknown>;
    frame_px?: number[] | null;
  };
  entities: IrEntity[];
  validation: {
    issues: IrValidationIssue[];
    coverage_recall: number | null;
    coverage_precision: number | null;
  };
  review: IrReviewItem[];
  parameters: {
    name: string;
    value: number;
    unit: "mm" | "deg" | "unitless";
    expression?: string | null;
  }[];
  constraints: {
    id: string;
    kind: string;
    refs: { entity_id: string; point: "p1" | "p2" | "center" }[];
    entity_ids: string[];
    value: number | null;
    parameter: string | null;
    tolerance: number;
    enabled: boolean;
  }[];
  recognizer_used: string | null;
}

export interface IrEnvelope {
  revision: number;
  origin: string;
  summary: Record<string, unknown>;
  ir: CadIr;
}

export interface ParamProvenance {
  origin: "measured" | "stated" | "guessed" | "propagated";
  detail: string;
  source_entity_id?: string | null;
  source_parameter?: string | null;
}

export interface Feature3D {
  kind: "extrude" | "hole" | "boss" | "pocket" | "fillet" | "chamfer";
  source_entity_ids: string[];
  params: Record<string, unknown>;
  param_provenance?: Record<string, ParamProvenance>;
  confidence: number;
}

export interface FeatureTreeCandidate {
  features: Feature3D[];
  score: number;
  label: string;
  missing_data: string[];
}

export interface FeatureParameterOverride {
  feature_index: number;
  depth_mm?: number;
  through?: boolean | null;
}

export interface AddedCadFeature {
  kind: "boss" | "pocket";
  profile: "circle" | "rectangle";
  center_x_mm: number;
  center_y_mm: number;
  depth_mm: number;
  diameter_mm?: number;
  width_mm?: number;
  height_mm?: number;
}

export interface AddedCadEdgeFeature {
  kind: "fillet" | "chamfer";
  edge_key: string;
  size_mm: number;
}

export type IrPatchOp =
  | { op: "confirm"; entity_id: string }
  | { op: "delete"; entity_id: string }
  | { op: "update"; entity_id: string; entity: Partial<IrEntity> }
  | { op: "add"; entity: Partial<IrEntity> }
  | { op: "set_scale"; scale: number }
  | { op: "set_sheet_format"; sheet_format: string }
  | { op: "set_title_block"; title_block: Record<string, string | number> }
  | { op: "move"; entity_id: string; dx: number; dy: number }
  | { op: "copy"; entity_id: string; dx?: number; dy?: number }
  | {
      op: "mirror";
      entity_id: string;
      mirror_p1: { x: number; y: number };
      mirror_p2: { x: number; y: number };
    }
  | {
      op: "fillet" | "chamfer";
      entity_id: string;
      entity_id_2: string;
      value: number;
    }
  | {
      op: "trim" | "extend";
      entity_id: string;
      entity_id_2: string;
      click_x: number;
      click_y: number;
    }
  | {
      op: "offset";
      entity_id: string;
      value: number;
      click_x: number;
      click_y: number;
    }
  | {
      op: "pattern_linear";
      entity_id: string;
      count: number;
      dx: number;
      dy: number;
    }
  | {
      op: "pattern_polar";
      entity_id: string;
      count: number;
      click_x: number;
      click_y: number;
      value: number;
    }
  | { op: "set_construction"; entity_id: string }
  | { op: "hatch_click"; click_x: number; click_y: number }
  | { op: "set_parameters"; parameters: CadIr["parameters"] }
  | { op: "set_constraints"; constraints: CadIr["constraints"] };

export async function getIr(id: string): Promise<IrEnvelope> {
  const res = await apiFetch(`${BASE}/${id}/ir`);
  return jsonOrThrow<IrEnvelope>(res);
}

export async function patchIr(
  id: string,
  ops: IrPatchOp[],
): Promise<IrEnvelope> {
  const res = await mutFetch(`${BASE}/${id}/ir`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ops }),
  });
  return jsonOrThrow<IrEnvelope>(res);
}

export async function revertIr(
  id: string,
  revision: number,
): Promise<IrEnvelope> {
  const res = await mutFetch(`${BASE}/${id}/ir/revert`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ revision }),
  });
  return jsonOrThrow<IrEnvelope>(res);
}

export async function solveIr(id: string): Promise<IrEnvelope> {
  const res = await mutFetch(`${BASE}/${id}/ir/solve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return jsonOrThrow<IrEnvelope>(res);
}

export interface ConstraintCheck {
  constraint_id: string;
  ok: boolean;
  message: string;
  entity_ids: string[];
}

export async function evaluateConstraints(
  id: string,
): Promise<{ checks: ConstraintCheck[]; violated: number }> {
  const res = await apiFetch(`${BASE}/${id}/ir/constraints/evaluate`);
  return jsonOrThrow<{ checks: ConstraintCheck[]; violated: number }>(res);
}

export interface ReleaseManifest {
  manifest_version: string;
  generation_id: string;
  revision: number;
  dxf_version: string;
  fully_reproducible: boolean;
  manifest_sha256: string;
  cad_ir: { sha256: string | null; reproducible: boolean };
  artifacts: Record<string, { reproducible: boolean }>;
  validation: { eskd_profile_version: string | null; issue_count: number };
  approval: { accepted_by: string | null; accepted_at: string | null };
}

export async function getReleaseManifest(id: string): Promise<ReleaseManifest> {
  const res = await apiFetch(`${BASE}/${id}/release-manifest`);
  return jsonOrThrow<ReleaseManifest>(res);
}

export function releasePackageUrl(id: string): string {
  return `${BASE}/${id}/release-package`;
}

export async function runFullCheck(id: string): Promise<IrEnvelope> {
  const res = await mutFetch(`${BASE}/${id}/ir/full-check`, { method: "POST" });
  return jsonOrThrow<IrEnvelope>(res);
}

export async function acceptVectorize(id: string): Promise<Generation> {
  const res = await mutFetch(`${BASE}/${id}/accept-vectorize`, {
    method: "POST",
  });
  return jsonOrThrow<Generation>(res);
}

export async function getFeatureTreeCandidates(
  id: string,
): Promise<FeatureTreeCandidate[]> {
  const res = await apiFetch(`${BASE}/${id}/ir/feature-tree-candidates`);
  const body = await jsonOrThrow<{ candidates: FeatureTreeCandidate[] }>(res);
  return body.candidates;
}

export async function compileFeatureTreeCandidate(
  id: string,
  index: number,
  confirmAssumptions: boolean,
  featureOverrides: FeatureParameterOverride[],
  addedFeatures: (AddedCadFeature | AddedCadEdgeFeature)[],
): Promise<void> {
  const res = await mutFetch(
    `${BASE}/${id}/ir/feature-tree-candidates/${index}/step`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        confirm_assumptions: confirmAssumptions,
        feature_overrides: featureOverrides,
        added_features: addedFeatures,
      }),
    },
  );
  if (!res.ok) {
    let detail = `${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      detail =
        typeof body.detail === "string"
          ? body.detail
          : JSON.stringify(body.detail ?? body);
    } catch {
      // Keep the status when the proxy returned a non-JSON error.
    }
    throw new Error(detail);
  }
}

export async function createBlankSheet(input: {
  format?: "A4" | "A3" | "A2" | "A1";
  landscape?: boolean;
  title?: string;
  case_id?: string;
  with_frame?: boolean;
  designation?: string;
  company?: string;
}): Promise<Generation> {
  const res = await mutFetch(`${BASE}/blank-sheet`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  return jsonOrThrow<Generation>(res);
}
