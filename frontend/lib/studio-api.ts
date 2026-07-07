"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const BASE = `${API}/api/image-gen`;

// "eskd" = text→image ЕСКД-styled diffusion (alternative to the deterministic
// techDraw() vector render, which is not a ComfyUI operation).
export type Operation = "generate" | "edit" | "inpaint" | "cleanup" | "eskd";
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

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = (body?.detail as string) || detail;
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

export async function setStudioJobPriority(id: string, priority: number): Promise<StudioJob> {
  const res = await mutFetch(`${API}/api/studio/queue/${id}/priority`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ priority }),
  });
  return jsonOrThrow<StudioJob>(res);
}

export async function bulkCancelStudioQueue(input: {
  resource?: string;
  owner_sub?: string;
} = {}): Promise<{ cancelled: number }> {
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

export function sourceUrl(id: string, index = 0): string {
  return `${BASE}/${id}/source?index=${index}`;
}

export function artifactUrl(id: string, kind: "dxf"): string {
  return `${BASE}/${id}/artifact?kind=${encodeURIComponent(kind)}`;
}
