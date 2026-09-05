/**
 * Обращения к API раздела «Модели».
 *
 * Экран собирал их вручную в шести местах (каждый компонент со своей функцией
 * `load()` на голом fetch + csrfHeaders), и ошибки обрабатывались по-разному:
 * где alert(JSON.stringify(detail)), где эфемерная строка, где молча. Здесь
 * один способ: не-2xx превращается в ApiError с кодом и текстом от сервера,
 * а вызывающий решает, показать его тостом или встроить в разметку.
 */

import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import type {
  AssignmentDraft,
  CatalogModel,
  KnownKind,
  ProviderInstance,
  RoutingChain,
  ModelCandidate,
  Slot,
  SlotSmokeResult,
  ThinkingLevel,
} from "./types";

const API = getApiBaseUrl();

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail || `HTTP ${status}`);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const isMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method);

  const res = await fetch(`${API}${path}`, {
    credentials: "include",
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(isMutation ? csrfHeaders() : {}),
      ...init?.headers,
    },
  });

  if (!res.ok) {
    // detail сервера бывает и строкой, и объектом — до сих пор его показывали
    // через JSON.stringify целиком, вместе с фигурными скобками.
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      const d = body?.detail ?? body;
      detail = typeof d === "string" ? d : (d?.message ?? JSON.stringify(d));
    } catch {
      /* тело не JSON — оставляем статус */
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ── Провайдеры (узлы) ────────────────────────────────────────────────────────

export const listProviders = () =>
  request<{ instances: ProviderInstance[]; known_kinds: KnownKind[] }>(
    "/api/providers",
  );

export const createProvider = (body: {
  kind: string;
  name: string;
  base_url?: string | null;
  api_key?: string;
}) => request<ProviderInstance>("/api/providers", { method: "POST", body: JSON.stringify(body) });

export const updateProvider = (
  id: string,
  body: Partial<{
    name: string;
    base_url: string | null;
    enabled: boolean;
    api_key: string;
    extra: Record<string, unknown>;
  }>,
) =>
  request<ProviderInstance>(`/api/providers/${id}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });

export const deleteProvider = (id: string) =>
  request<{ ok: boolean }>(`/api/providers/${id}`, { method: "DELETE" });

export const testProvider = (id: string) =>
  request<{ ok: boolean; error?: string | null; model_count?: number }>(
    `/api/providers/${id}/test`,
    { method: "POST" },
  );

export const refreshProviderModels = (id: string) =>
  request<{ added: string[]; count: number }>(
    `/api/providers/${id}/refresh-models`,
    { method: "POST" },
  );

// ── Каталог и маршруты ───────────────────────────────────────────────────────

export const listCatalogModels = (opts?: { includeDisabled?: boolean }) =>
  request<CatalogModel[]>(
    `/api/providers/models?include_disabled=${opts?.includeDisabled ? "true" : "false"}`,
  );

export const routingHealth = () =>
  request<RoutingChain[]>("/api/providers/routing-health");

export const pruneRouting = () =>
  request<{ pruned: string[]; skipped_head: string[]; failed: string[] }>(
    "/api/providers/routing-health/prune",
    { method: "POST" },
  );

// ── Слоты и назначения ───────────────────────────────────────────────────────

export const listSlots = () => request<{ slots: Slot[] }>("/api/providers/slots");

export const getAssignmentDraft = () =>
  request<AssignmentDraft>("/api/providers/assignment-draft");

export const validateAssignmentDraft = (slots: Record<string, string | null>) =>
  request<AssignmentDraft>("/api/providers/assignment-draft/validate", {
    method: "POST",
    body: JSON.stringify({ slots }),
  });

export const applyAssignmentDraft = (
  slots: Record<string, string | null>,
  confirmWarnings = false,
) =>
  request<AssignmentDraft>("/api/providers/assignment-draft/apply", {
    method: "POST",
    body: JSON.stringify({ slots, confirm_warnings: confirmWarnings }),
  });

export const rollbackAssignment = (revisionId: string) =>
  request<AssignmentDraft>(`/api/providers/assignments/${revisionId}/rollback`, {
    method: "POST",
  });

export const setSlotThinking = (
  slot: string,
  body: { enabled: boolean | null; level?: ThinkingLevel | null },
) =>
  request<Slot>(`/api/providers/slots/${slot}/thinking`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export const setSlotAllowCloud = (slot: string, allowed: boolean) =>
  request<{ ok: boolean; slot: string; cloud_allowed: boolean }>(
    `/api/providers/slots/${slot}/allow-cloud`,
    { method: "PATCH", body: JSON.stringify({ allowed }) },
  );

/**
 * Пробный вызов. `dry_run` проверяет пару слот/модель без обращения к
 * провайдеру, поэтому годится как предпросмотр черновика; без него делается
 * один настоящий запрос — у облака это стоит денег.
 */
export const smokeSlot = (
  slot: string,
  body: {
    model?: string | null;
    thinking?: boolean | null;
    thinking_level?: ThinkingLevel | null;
    dry_run?: boolean;
  } = {},
) =>
  request<SlotSmokeResult>(`/api/providers/slots/${slot}/smoke`, {
    method: "POST",
    body: JSON.stringify({ dry_run: true, ...body }),
  });

export const setModelPreferredInstance = (
  modelKey: string,
  instance: string | null,
) =>
  request<CatalogModel>(`/api/providers/models/${modelKey}/preferred-instance`, {
    method: "PATCH",
    body: JSON.stringify({ preferred_instance: instance }),
  });

export const setModelThinking = (
  modelKey: string,
  body: { enabled: boolean; level?: ThinkingLevel | null },
) =>
  request<CatalogModel>(`/api/providers/models/${modelKey}/thinking`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

/** Кандидаты для слота с вердиктом пригодности, посчитанным сервером. */
export const listSlotCandidates = (slot: string) =>
  request<ModelCandidate[]>(`/api/providers/slots/${slot}/candidates`);
