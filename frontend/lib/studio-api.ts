"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const BASE = `${API}/api/image-gen`;

export type Operation = "generate" | "edit" | "inpaint" | "cleanup";
export type GenStatus = "queued" | "running" | "done" | "failed";

export interface Generation {
  id: string;
  operation: Operation;
  status: GenStatus;
  prompt: string | null;
  negative_prompt: string | null;
  params: Record<string, unknown>;
  source_image_paths: string[];
  mask_path: string | null;
  has_result: boolean;
  error: string | null;
  parent_id: string | null;
  accepted: boolean;
  workflow_id: string | null;
  created_at: string | null;
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

/** URL for the result/thumbnail/source image (served by the backend, auth via cookie). */
export function resultUrl(id: string, thumb = false): string {
  return `${BASE}/${id}/result${thumb ? "?thumb=true" : ""}`;
}

export function sourceUrl(id: string, index = 0): string {
  return `${BASE}/${id}/source?index=${index}`;
}
