import { csrfHeaders } from "@/lib/auth";
import { getApiBaseUrl } from "@/lib/api-base";
import type { SketchProfileSegment } from "@/lib/cad-sketch-api";

export type EngineeringProject = {
  id: string;
  name: string;
  code: string | null;
  status: "draft" | "validated" | "needs_review" | "approved" | "obsolete";
  description: string | null;
  created_at: string;
  updated_at: string;
};

export type EngineeringRevision = {
  id: string;
  engineering_project_id: string;
  revision: number;
  base_revision: number | null;
  status: "validated" | "needs_review" | "approved";
  origin: string;
  change_summary: string | null;
  validation: {
    issues?: Array<{ severity?: string; code?: string; message_ru?: string }>;
  };
  created_at: string;
};

export type EngineeringProjectDetail = EngineeringProject & {
  revisions: EngineeringRevision[];
};

export type EngineeringAnalysisCase = {
  id: string;
  engineering_revision_id: string;
  name: string;
  analysis_type: string;
  status: string;
  inputs: Record<string, unknown>;
  results: Record<string, number | null>;
  executed_at: string | null;
};

export type EngineeringMaterial = {
  id: string;
  designation: string;
  standard: string | null;
  density_kg_m3: number | null;
};

// A revision-safe link from an engineering revision to a domain artifact
// (a CAD IR snapshot, a drawing, a BOM, a manufacturing process plan).
// `state` is "current" until a newer canonical revision supersedes it,
// then "stale".
export type EngineeringProjectionEntityType =
  "cad_ir_revision" | "drawing" | "bom" | "manufacturing_process_plan";

export type EngineeringProjection = {
  id: string;
  engineering_revision_id: string;
  projection_type: string;
  entity_type: EngineeringProjectionEntityType | string;
  entity_id: string;
  state: "current" | "stale" | string;
  metadata: Record<string, unknown>;
  created_at: string;
};

export type EngineeringModelGraphRevision = {
  id: string;
  engineering_project_id?: string | null;
  engineering_revision_id?: string | null;
  graph_id: string;
  revision: number;
  parent_revision: number | null;
  canonical_sha256: string;
  profile: string;
  comprehension_status: string;
  build_status: string;
  release_status: string;
  generation_id?: string;
  source_generation_status?: string;
  workflow_status?: string;
  graph: {
    nodes: Array<{ id: string; type: string; name?: string | null }>;
    edges: Array<{
      id: string;
      type: string;
      source_id: string;
      target_id: string;
    }>;
    assertions: Array<{
      id: string;
      subject_id: string;
      predicate: string;
      value: Record<string, unknown>;
      origin: string;
      assurance: string;
      confidence: number;
      impacts: string[];
      evidence_ids: string[];
      state: string;
      hypothesis_id?: string | null;
      supersedes_assertion_id?: string | null;
    }>;
    evidence: Array<{
      id: string;
      kind: string;
      source_region_id?: string | null;
      payload: Record<string, unknown>;
    }>;
    hypothesis_sets: Array<{
      id: string;
      option_ids: string[];
      selected_option_id?: string | null;
    }>;
    build_targets: Array<{
      id: string;
      kind: string;
      root_node_ids: string[];
      requirement_ids: string[];
      critical_impacts: string[];
      mass_tolerance_percent: number;
    }>;
    verification: {
      critical_unresolved_assertion_ids: string[];
      issue_codes: string[];
    };
    reader_manifest: {
      max_wall_seconds: number;
      max_model_calls: number;
      call_timeout_seconds: number;
      no_progress_pass_limit: number;
      calls_used: number;
      elapsed_seconds: number;
      no_progress_passes: number;
      ordinary_attempts: Record<string, number>;
      stop_reason?: string | null;
    };
  };
};

export type EngineeringAssertionImpact = {
  assertion_id: string;
  target_id: string;
  subject_node_id: string;
  critical_for_target: boolean;
  classification: "critical_for_target" | "non_critical_for_target";
  declared_impacts: string[];
  direct_dependency_node_ids: string[];
  affected_node_ids: string[];
  affected_build_operation_ids: string[];
  affected_artifact_ids: string[];
  affected_topology_element_ids: string[];
  evidence_ids: string[];
  superseded_by_assertion_ids: string[];
  dependency_paths: Record<string, string[]>;
};

export type EngineeringGraphPatch = {
  id: string;
  patch_id: string;
  producer: string;
  pass_id: string;
  accepted: boolean;
  payload: Record<string, unknown>;
  validation_errors: string[];
  created_at: string;
};

export type EngineeringTraceProposal = {
  id: string;
  proposal_id: string;
  source_region_id: string;
  assertion_id: string | null;
  rank: number;
  status: string;
  score: number | null;
  payload: Record<string, unknown>;
  visual_verifications: Array<Record<string, unknown>>;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.method && init.method !== "GET" ? csrfHeaders() : {}),
      ...init?.headers,
    },
  });
  if (!response.ok)
    throw new Error(`${response.status} ${await response.text()}`);
  return response.json() as Promise<T>;
}

// Mirrors AddedFeatureRequest in image_generation.py — same shape the old
// Cad3dPanel add-feature form already used, now sent to the graph-native
// endpoint instead of the 2D-IR candidate-index one.
export interface AddedNativeFeature {
  kind: "boss" | "pocket" | "fillet" | "chamfer" | "shell" | "thread";
  profile?: "circle" | "rectangle" | "sketch" | null;
  center_x_mm?: number | null;
  center_y_mm?: number | null;
  depth_mm?: number | null;
  diameter_mm?: number | null;
  width_mm?: number | null;
  height_mm?: number | null;
  // Ф4: a solved sketch's own closed-loop wire.
  sketch_profile?: SketchProfileSegment[] | null;
  edge_key?: string | null;
  size_mm?: number | null;
  thickness_mm?: number | null;
  spec?: string | null;
  pitch_mm?: number | null;
}

export type EngineeringDomainArtifactBuild = {
  graph_id: string;
  revision: number;
  canonical_sha256: string;
  artifact_path: string;
  artifact_sha256: string;
  report_path: string;
  views?: string[];
  production_export_allowed: boolean;
  critical_assumption_ids: string[];
  idempotent_replay: boolean;
};

function apiUrl(path: string): string {
  return `${getApiBaseUrl()}${path}`;
}

export const engineeringApi = {
  listProjects: () =>
    request<EngineeringProject[]>("/api/engineering/projects"),
  createProject: (body: {
    name: string;
    code?: string;
    description?: string;
  }) =>
    request<EngineeringProject>("/api/engineering/projects", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  getProject: (projectId: string) =>
    request<EngineeringProjectDetail>(`/api/engineering/projects/${projectId}`),
  createRevision: (
    projectId: string,
    body: { base_revision: number | null; change_summary?: string },
  ) =>
    request<EngineeringRevision>(
      `/api/engineering/projects/${projectId}/revisions`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  validateRevision: (revisionId: string) =>
    request<{ status: string; summary: { total: number; errors: number } }>(
      `/api/engineering/revisions/${revisionId}/validate`,
      { method: "POST" },
    ),
  listMaterials: () =>
    request<EngineeringMaterial[]>("/api/engineering/materials"),
  listAnalysisCases: (revisionId: string) =>
    request<EngineeringAnalysisCase[]>(
      `/api/engineering/revisions/${revisionId}/analysis-cases`,
    ),
  createAnalysisCase: (
    revisionId: string,
    body: {
      name: string;
      material_id?: string;
      inputs: Record<string, number>;
    },
  ) =>
    request<EngineeringAnalysisCase>(
      `/api/engineering/revisions/${revisionId}/analysis-cases`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  runAnalysisCase: (caseId: string) =>
    request<EngineeringAnalysisCase>(
      `/api/engineering/analysis-cases/${caseId}/run`,
      { method: "POST" },
    ),
  listProjections: (revisionId: string) =>
    request<EngineeringProjection[]>(
      `/api/engineering/revisions/${revisionId}/projections`,
    ),
  createProjection: (
    revisionId: string,
    body: {
      projection_type: string;
      entity_type: string;
      entity_id: string;
      metadata?: Record<string, unknown>;
    },
  ) =>
    request<EngineeringProjection>(
      `/api/engineering/revisions/${revisionId}/projections`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  listModelGraphs: (projectId: string) =>
    request<EngineeringModelGraphRevision[]>(
      `/api/engineering-model-graphs/projects/${projectId}/graphs`,
    ),
  getGenerationModelGraph: (generationId: string) =>
    request<EngineeringModelGraphRevision>(
      `/api/image-gen/${generationId}/model-graph`,
    ),
  generationModelGraphDownloadUrl: (generationId: string) =>
    `${getApiBaseUrl()}/api/image-gen/${generationId}/model-graph/download`,
  listGenerationGraphPatches: (generationId: string) =>
    request<EngineeringGraphPatch[]>(
      `/api/image-gen/${generationId}/model-graph/patches`,
    ),
  listGenerationTraceProposals: (generationId: string) =>
    request<EngineeringTraceProposal[]>(
      `/api/image-gen/${generationId}/model-graph/trace-proposals`,
    ),
  getGenerationAssertionImpact: (
    generationId: string,
    assertionId: string,
    targetId: string,
  ) =>
    request<EngineeringAssertionImpact>(
      `/api/image-gen/${generationId}/model-graph/assertions/${encodeURIComponent(assertionId)}/impact?target_id=${encodeURIComponent(targetId)}`,
    ),
  generationAssertionOverlayUrl: (
    generationId: string,
    assertionId: string,
    mode:
      "sheet" | "source" | "candidate" | "overlay" | "difference" = "source",
    proposalId?: string,
  ) => {
    const query = new URLSearchParams({ mode });
    if (proposalId) query.set("proposal_id", proposalId);
    return `${getApiBaseUrl()}/api/image-gen/${generationId}/model-graph/assertions/${encodeURIComponent(assertionId)}/source-overlay?${query}`;
  },
  correctGenerationAssertion: (
    generationId: string,
    assertionId: string,
    body: {
      value: Record<string, unknown>;
      unit?: string | null;
      note: string;
      idempotency_key: string;
      source_bbox_normalized?: [number, number, number, number] | null;
      rebuild?: boolean;
    },
  ) =>
    request<
      EngineeringModelGraphRevision & {
        compatibility_spec_updated: boolean;
        rebuild_task_id?: string | null;
      }
    >(
      `/api/image-gen/${generationId}/model-graph/assertions/${encodeURIComponent(assertionId)}/corrections`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  correctGenerationAssertionsBatch: (
    generationId: string,
    body: {
      corrections: Array<{
        assertion_id: string;
        value: Record<string, unknown>;
        unit?: string | null;
        source_bbox_normalized?: [number, number, number, number] | null;
      }>;
      note: string;
      idempotency_key: string;
      rebuild?: boolean;
    },
  ) =>
    request<
      EngineeringModelGraphRevision & {
        compatibility_spec_updated: boolean;
        corrected_assertion_ids: string[];
        rebuild_task_id?: string | null;
      }
    >(`/api/image-gen/${generationId}/model-graph/corrections`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Ф2 нового CAD-редактора: appends a new BuildOperation onto the
  // graph's CURRENT state (not a re-read spec) and always recompiles —
  // see AddNativeFeatureRequest's own docstring in image_generation.py.
  addGenerationModelGraphFeature: (
    generationId: string,
    body: {
      feature: AddedNativeFeature;
      note: string;
      idempotency_key: string;
    },
  ) =>
    request<{ generation_id: string; rebuild_task_id: string }>(
      `/api/image-gen/${generationId}/model-graph/features`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  verifyGenerationModelGraph: (generationId: string) =>
    request<{
      state: Record<string, unknown>;
      issues: Array<Record<string, unknown>>;
    }>(`/api/image-gen/${generationId}/model-graph/verify`, { method: "POST" }),
  startGenerationModelReader: (generationId: string, targetId = "preview") =>
    request<{
      task_id: string;
      graph_id: string;
      base_revision: number;
      target_id: string;
    }>(
      `/api/image-gen/${generationId}/model-graph/reader-runs?target_id=${encodeURIComponent(targetId)}`,
      { method: "POST" },
    ),
  listGraphPatches: (graphId: string) =>
    request<EngineeringGraphPatch[]>(
      `/api/engineering-model-graphs/graphs/${encodeURIComponent(graphId)}/patches`,
    ),
  listTraceProposals: (revisionId: string) =>
    request<EngineeringTraceProposal[]>(
      `/api/engineering-model-graphs/revisions/${revisionId}/trace-proposals`,
    ),
  getAssertionImpact: (
    revisionId: string,
    assertionId: string,
    targetId: string,
  ) =>
    request<EngineeringAssertionImpact>(
      `/api/engineering-model-graphs/revisions/${revisionId}/assertions/${encodeURIComponent(assertionId)}/impact?target_id=${encodeURIComponent(targetId)}`,
    ),
  verifyModelGraph: (revisionId: string) =>
    request<{
      state: Record<string, unknown>;
      issues: Array<Record<string, unknown>>;
    }>(`/api/engineering-model-graphs/revisions/${revisionId}/verify`, {
      method: "POST",
    }),
  startModelReader: (graphId: string, targetId = "preview") =>
    request<{
      task_id: string;
      graph_id: string;
      base_revision: number;
      target_id: string;
    }>(
      `/api/engineering-model-graphs/graphs/${encodeURIComponent(graphId)}/reader-runs?target_id=${encodeURIComponent(targetId)}`,
      { method: "POST" },
    ),
  getTaskStatus: (taskId: string) =>
    request<{
      task_id: string;
      status: string;
      result?: Record<string, unknown> | null;
    }>(`/api/tasks/${taskId}`),
  buildAssemblyDrawing: (assemblyId: string) =>
    request<EngineeringDomainArtifactBuild>(
      `/api/engineering/assemblies/${assemblyId}/model-graph/drawing`,
      { method: "POST" },
    ),
  buildConstructionSheets: (revisionId: string) =>
    request<EngineeringDomainArtifactBuild>(
      `/api/engineering/revisions/${revisionId}/construction-model-graph/sheets`,
      { method: "POST" },
    ),
  buildSystemDiagram: (revisionId: string) =>
    request<EngineeringDomainArtifactBuild>(
      `/api/engineering/revisions/${revisionId}/system-model-graph/diagram`,
      { method: "POST" },
    ),
  graphArtifactUrl: (
    revisionId: string,
    artifactId: string,
    kind: "artifact" | "report" = "artifact",
  ) =>
    apiUrl(
      `/api/engineering-model-graphs/revisions/${revisionId}/artifacts/${encodeURIComponent(artifactId)}?kind=${kind}`,
    ),
};
