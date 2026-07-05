"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const BASE = `${API}/api/lora`;

export type LoraDatasetStatus = "preparing" | "ready" | "failed";
export type LoraRunStatus =
  | "pending_approval"
  | "queued"
  | "running"
  | "stopping"
  | "done"
  | "failed"
  | "cancelled";

export interface LoraDatasetStats {
  sources?: number;
  rendered?: number;
  synthetic?: number;
  captioned?: number;
  caption_rejected?: number;
  pairs?: number;
  holdout?: number;
  page_skipped?: number;
  pair_rejected?: string[];
  render_failed?: string[];
}

export interface LoraDataset {
  id: string;
  name: string;
  status: LoraDatasetStatus;
  preset: string;
  params: Record<string, unknown>;
  stats: LoraDatasetStats;
  preview_paths: string[];
  error: string | null;
  created_at: string | null;
}

export interface LoraProgress {
  step?: number;
  total?: number;
  loss?: number | null;
  eta?: string | null;
  phase?: string;
  history?: [number, number][];
  ts?: number;
}

export interface LoraRun {
  id: string;
  dataset_id: string;
  name: string;
  status: LoraRunStatus;
  config: Record<string, unknown>;
  base_family: string;
  eta_hours: number | null;
  progress: LoraProgress;
  checkpoints: string[];
  sample_paths: string[];
  control_paths: string[];
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface LoraBaseModel {
  key: string;
  label: string;
  family: string;
  fits_24gb: boolean;
  vram_note: string | null;
  sec_per_step: number | null;
  gated: boolean;
}

export async function uploadSource(file: File): Promise<{ path: string }> {
  const form = new FormData();
  form.append("file", file);
  const res = await mutFetch(`${BASE}/upload`, { method: "POST", body: form });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
  return res.json();
}

export async function listDatasets(): Promise<LoraDataset[]> {
  const res = await apiFetch(`${BASE}/datasets`);
  if (!res.ok) throw new Error(res.statusText);
  return (await res.json()).datasets;
}

export async function createDataset(body: {
  name: string;
  preset?: string;
  source_paths: string[];
  params: Record<string, unknown>;
}): Promise<LoraDataset> {
  const res = await mutFetch(`${BASE}/datasets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
  return res.json();
}

export async function listRuns(): Promise<LoraRun[]> {
  const res = await apiFetch(`${BASE}/runs`);
  if (!res.ok) throw new Error(res.statusText);
  return (await res.json()).runs;
}

export async function createRun(body: {
  dataset_id: string;
  name: string;
  config: Record<string, unknown>;
}): Promise<LoraRun> {
  const res = await mutFetch(`${BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
  return res.json();
}

export async function stopRun(id: string): Promise<void> {
  const res = await mutFetch(`${BASE}/runs/${id}/stop`, { method: "POST" });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
}

export async function makeWorkflow(
  id: string,
  checkpoint: string,
  strength = 1.0,
): Promise<{ workflow_id: string; title: string; lora_name: string }> {
  const res = await mutFetch(`${BASE}/runs/${id}/make-workflow`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ checkpoint, strength }),
  });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
  return res.json();
}

export async function deployCheckpoint(
  id: string,
  checkpoint: string,
): Promise<{ lora_name: string }> {
  const res = await mutFetch(
    `${BASE}/runs/${id}/deploy?checkpoint=${encodeURIComponent(checkpoint)}`,
    { method: "POST" },
  );
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
  return res.json();
}

export async function deleteDataset(id: string): Promise<void> {
  const res = await mutFetch(`${BASE}/datasets/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
}

export async function deleteRun(id: string): Promise<void> {
  const res = await mutFetch(`${BASE}/runs/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
}

export async function listCaptionModels(): Promise<
  { key: string; model: string; provider: string }[]
> {
  const res = await apiFetch(`${BASE}/caption-models`);
  if (!res.ok) throw new Error(res.statusText);
  return (await res.json()).models;
}

export async function listBaseModels(): Promise<{
  models: LoraBaseModel[];
  default: string;
}> {
  const res = await apiFetch(`${BASE}/base-models`);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

export interface HfTokenStatus {
  configured: boolean;
  masked: string;
  source: "settings" | "env" | null;
}

export async function getHfTokenStatus(): Promise<HfTokenStatus> {
  const res = await apiFetch(`${BASE}/hf-token`);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

export async function setHfToken(token: string): Promise<HfTokenStatus> {
  const res = await mutFetch(`${BASE}/hf-token`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
  return res.json();
}

export interface GpuStatus {
  training_lock: { run_id?: string } | null;
  ts: number;
}

export async function gpuStatus(): Promise<GpuStatus> {
  const res = await apiFetch(`${BASE}/gpu-status`);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

export function previewUrl(path: string): string {
  return `${BASE}/preview?path=${encodeURIComponent(path)}`;
}
