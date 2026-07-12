import { csrfHeaders } from "@/lib/auth";
import { getApiBaseUrl } from "@/lib/api-base";

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
  validation: { issues?: Array<{ severity?: string; code?: string; message_ru?: string }> };
  created_at: string;
};

export type EngineeringProjectDetail = EngineeringProject & { revisions: EngineeringRevision[] };

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
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  return response.json() as Promise<T>;
}

export const engineeringApi = {
  listProjects: () => request<EngineeringProject[]>("/api/engineering/projects"),
  createProject: (body: { name: string; code?: string; description?: string }) =>
    request<EngineeringProject>("/api/engineering/projects", { method: "POST", body: JSON.stringify(body) }),
  getProject: (projectId: string) => request<EngineeringProjectDetail>(`/api/engineering/projects/${projectId}`),
  createRevision: (projectId: string, body: { base_revision: number | null; change_summary?: string }) =>
    request<EngineeringRevision>(`/api/engineering/projects/${projectId}/revisions`, { method: "POST", body: JSON.stringify(body) }),
  validateRevision: (revisionId: string) =>
    request<{ status: string; summary: { total: number; errors: number } }>(`/api/engineering/revisions/${revisionId}/validate`, { method: "POST" }),
  listMaterials: () => request<EngineeringMaterial[]>("/api/engineering/materials"),
  listAnalysisCases: (revisionId: string) => request<EngineeringAnalysisCase[]>(`/api/engineering/revisions/${revisionId}/analysis-cases`),
  createAnalysisCase: (revisionId: string, body: { name: string; material_id?: string; inputs: Record<string, number> }) =>
    request<EngineeringAnalysisCase>(`/api/engineering/revisions/${revisionId}/analysis-cases`, { method: "POST", body: JSON.stringify(body) }),
  runAnalysisCase: (caseId: string) =>
    request<EngineeringAnalysisCase>(`/api/engineering/analysis-cases/${caseId}/run`, { method: "POST" }),
};
