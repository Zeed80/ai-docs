/**
 * REST API client for FastAPI backend.
 * Used for all CRUD operations (degraded mode compatible).
 */

import { getApiBaseUrl } from "@/lib/api-base";

const API_BASE = getApiBaseUrl();

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });

  if (!res.ok) {
    const body = await res.text();
    throw new ApiError(res.status, body);
  }

  return res.json();
}

// ── Documents ───────────────────────────────────────────────────────────────

export interface Document {
  id: string;
  file_name: string;
  file_hash: string;
  file_size: number;
  mime_type: string;
  storage_path: string;
  page_count: number | null;
  doc_type: string | null;
  doc_type_confidence: number | null;
  status: string;
  source_channel: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  extractions: Extraction[];
  links: DocumentLink[];
}

export interface Extraction {
  id: string;
  model_name: string;
  overall_confidence: number | null;
  fields: ExtractionField[];
  created_at: string;
}

export interface ExtractionField {
  field_name: string;
  field_value: string | null;
  confidence: number | null;
  confidence_reason: string | null;
  bbox_page: number | null;
  bbox_x: number | null;
  bbox_y: number | null;
  bbox_w: number | null;
  bbox_h: number | null;
  human_corrected: boolean;
  corrected_value: string | null;
}

export interface DocumentLink {
  id: string;
  linked_entity_type: string;
  linked_entity_id: string;
  link_type: string;
}

export interface DocumentListResponse {
  items: Document[];
  total: number;
  offset: number;
  limit: number;
}

export interface DocumentPipelineStatus {
  processing_status: string | null;
  current_step: string | null;
  processing_error: string | null;
  extraction_count: number;
  artifact_count: number;
  graph_status: string | null;
  graph_scope: string | null;
  graph_error: string | null;
  memory_chunks: number;
  evidence_spans: number;
  graph_nodes: number;
  graph_edges: number;
  graph_review_pending: number;
  embedding_records: number;
  ntd_checks: number;
  ntd_open_findings: number;
}

export interface DocumentWorkspaceItem {
  document: Document;
  pipeline: DocumentPipelineStatus;
}

export interface DocumentWorkspaceResponse {
  items: DocumentWorkspaceItem[];
  total: number;
  offset: number;
  limit: number;
  status_counts: Record<string, number>;
  doc_type_counts: Record<string, number>;
}

export interface DocumentManagementSummary {
  document: Document;
  pipeline: DocumentPipelineStatus;
  links: DocumentLink[];
}

export interface DocumentBatchActionResponse {
  action: string;
  results: Array<{
    document_id: string;
    status: string;
    task_id: string | null;
    detail: string | null;
  }>;
}

export const documents = {
  list: (params?: Record<string, string>) =>
    request<DocumentListResponse>(
      `/api/documents?${new URLSearchParams(params)}`,
    ),

  workspace: (params?: Record<string, string>) =>
    request<DocumentWorkspaceResponse>(
      `/api/documents/workspace?${new URLSearchParams(params)}`,
    ),

  get: (id: string) => request<Document>(`/api/documents/${id}`),

  management: (id: string) =>
    request<DocumentManagementSummary>(`/api/documents/${id}/management`),

  update: (id: string, data: Partial<Document>) =>
    request<Document>(`/api/documents/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  ingest: (file: File, sourceChannel = "upload") => {
    const formData = new FormData();
    formData.append("file", file);
    return fetch(
      `${API_BASE}/api/documents/ingest?source_channel=${sourceChannel}`,
      { method: "POST", body: formData },
    ).then((r) => {
      if (!r.ok && r.status !== 202) throw new ApiError(r.status, "Upload failed");
      return r.json();
    });
  },

  link: (
    id: string,
    data: {
      linked_entity_type: string;
      linked_entity_id: string;
      link_type?: string;
    },
  ) =>
    request<DocumentLink>(`/api/documents/${id}/links`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  delete: (id: string) =>
    request(`/api/documents/${id}`, {
      method: "DELETE",
    }),

  batchProcess: (documentIds: string[], force = false) =>
    request<DocumentBatchActionResponse>("/api/documents/batch/process", {
      method: "POST",
      body: JSON.stringify({ document_ids: documentIds, force }),
    }),

  batchClassify: (documentIds: string[], force = false) =>
    request<DocumentBatchActionResponse>("/api/documents/batch/classify", {
      method: "POST",
      body: JSON.stringify({ document_ids: documentIds, force }),
    }),

  batchMemoryRebuild: (documentIds: string[], buildScope = "extended") =>
    request<DocumentBatchActionResponse>("/api/documents/batch/memory-rebuild", {
      method: "POST",
      body: JSON.stringify({ document_ids: documentIds, build_scope: buildScope }),
    }),

  batchEmbeddingsReindex: (documentIds: string[]) =>
    request<DocumentBatchActionResponse>("/api/documents/batch/embeddings-reindex", {
      method: "POST",
      body: JSON.stringify({ document_ids: documentIds }),
    }),

  batchNtdCheck: (documentIds: string[]) =>
    request<DocumentBatchActionResponse>("/api/documents/batch/ntd-check", {
      method: "POST",
      body: JSON.stringify({ document_ids: documentIds }),
    }),

  search: (q: string, limit = 20) =>
    request<Document[]>(
      `/api/search/documents?q=${encodeURIComponent(q)}&limit=${limit}`,
      {
        method: "POST",
      },
    ),
};

// ── NTD / Norm Control ─────────────────────────────────────────────────────

export interface NTDFinding {
  id: string;
  check_id: string;
  document_id: string;
  normative_document_id: string | null;
  clause_id: string | null;
  requirement_id: string | null;
  severity: string;
  status: string;
  finding_code: string;
  message: string;
  evidence_text: string | null;
  recommendation: string | null;
  confidence: number;
  created_at: string;
}

export interface NTDCheck {
  id: string;
  document_id: string;
  status: string;
  mode: string;
  triggered_by: string;
  summary: string | null;
  findings_total: number;
  findings_open: number;
  created_at: string;
}

export interface NTDCheckDetail {
  check: NTDCheck;
  findings: NTDFinding[];
}

export interface NTDCheckAvailability {
  document_id: string;
  can_check: boolean;
  reasons: string[];
  active_requirements: number;
  has_text: boolean;
  mode: "manual" | "auto";
}

export interface EmbeddingProfile {
  model_key: string;
  provider_model: string;
  collection_name: string;
  dimension: number;
  distance_metric: string;
  normalize: boolean;
}

export interface EmbeddingStats {
  active_model: string;
  active_collection: string;
  dimension: number;
  counts_by_status: Record<string, number>;
  total: number;
}

export interface EmbeddingRebuildResult {
  created: number;
  stale_marked: number;
}

export const ntd = {
  listChecks: (documentId: string) =>
    request<NTDCheck[]>(`/api/documents/${documentId}/ntd-checks`),

  availability: (documentId: string) =>
    request<NTDCheckAvailability>(
      `/api/documents/${documentId}/ntd-check/availability`,
    ),

  runCheck: (documentId: string) =>
    request<NTDCheckDetail>(`/api/documents/${documentId}/ntd-check`, {
      method: "POST",
      body: JSON.stringify({
        document_id: documentId,
        triggered_by: "manual",
        actor: "user",
      }),
    }),
};

// ── Approvals ───────────────────────────────────────────────────────────────

export interface Approval {
  id: string;
  action_type: string;
  entity_type: string;
  entity_id: string;
  status: string;
  requested_by: string | null;
  assigned_to: string | null;
  context: Record<string, unknown> | null;
  decision_comment: string | null;
  decided_at: string | null;
  decided_by: string | null;
  created_at: string;
}

export interface ApprovalListResponse {
  items: Approval[];
  total: number;
}

export const approvals = {
  listPending: () => request<ApprovalListResponse>("/api/approvals/pending"),

  get: (id: string) => request<Approval>(`/api/approvals/${id}`),

  decide: (
    id: string,
    data: { status: string; comment?: string; decided_by?: string },
  ) =>
    request<Approval>(`/api/approvals/${id}/decide`, {
      method: "POST",
      body: JSON.stringify(data),
    }),
};

// ── Invoices ────────────────────────────────────────────────────────────────

export interface Invoice {
  id: string;
  document_id: string;
  invoice_number: string | null;
  invoice_date: string | null;
  due_date: string | null;
  currency: string;
  total_amount: number | null;
  status: string;
  overall_confidence: number | null;
  lines: InvoiceLine[];
  created_at: string;
}

export interface InvoiceLine {
  id: string;
  line_number: number;
  description: string | null;
  quantity: number | null;
  unit: string | null;
  unit_price: number | null;
  amount: number | null;
}

export interface InvoiceListResponse {
  items: Invoice[];
  total: number;
  offset: number;
  limit: number;
}

export interface ValidationError {
  field: string;
  error_type: string;
  message: string;
  expected: string | null;
  actual: string | null;
  severity: string;
}

export interface ValidationResponse {
  invoice_id: string;
  is_valid: boolean;
  errors: ValidationError[];
  overall_confidence: number | null;
}

export interface TaskResponse {
  task_id: string;
  document_id: string;
  status: string;
}

export const invoices = {
  list: (params?: Record<string, string>) =>
    request<InvoiceListResponse>(
      `/api/invoices?${new URLSearchParams(params)}`,
    ),

  get: (id: string) => request<Invoice>(`/api/invoices/${id}`),

  update: (id: string, data: Partial<Invoice>) =>
    request<Invoice>(`/api/invoices/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  validate: (id: string) =>
    request<ValidationResponse>(`/api/invoices/${id}/validate`, {
      method: "POST",
    }),

  approve: (id: string, comment?: string) =>
    request<Invoice>(`/api/invoices/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ comment }),
    }),

  reject: (id: string, reason: string) =>
    request<Invoice>(`/api/invoices/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
};

// ── Extraction ──────────────────────────────────────────────────────────────

export const extraction = {
  classify: (documentId: string) =>
    request<TaskResponse>(`/api/documents/${documentId}/classify`, {
      method: "POST",
    }),

  extract: (documentId: string) =>
    request<TaskResponse>(`/api/documents/${documentId}/extract`, {
      method: "POST",
    }),

  correctField: (
    documentId: string,
    data: { field_name: string; corrected_value: string },
  ) =>
    request<{
      field_name: string;
      old_value: string | null;
      corrected_value: string;
      extraction_id: string;
    }>(`/api/documents/${documentId}/correct-field`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  taskStatus: (taskId: string) =>
    request<{ task_id: string; status: string; result?: unknown }>(
      `/api/tasks/${taskId}`,
    ),
};

// ── Normalization ───────────────────────────────────────────────────────────

export interface NormRule {
  id: string;
  field_name: string;
  pattern: string;
  replacement: string;
  is_regex: boolean;
  status: string;
  source_corrections: number;
  suggested_by: string;
  activated_by: string | null;
  activated_at: string | null;
  apply_count: number;
  last_applied_at: string | null;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface NormRuleListResponse {
  items: NormRule[];
  total: number;
}

export const normalization = {
  listRules: (params?: Record<string, string>) =>
    request<NormRuleListResponse>(
      `/api/normalization/rules?${new URLSearchParams(params)}`,
    ),

  createRule: (data: {
    field_name: string;
    pattern: string;
    replacement: string;
    is_regex?: boolean;
    description?: string;
  }) =>
    request<NormRule>("/api/normalization/rules", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  activateRule: (id: string, activatedBy = "user") =>
    request<NormRule>(`/api/normalization/rules/${id}/activate`, {
      method: "POST",
      body: JSON.stringify({ activated_by: activatedBy }),
    }),

  disableRule: (id: string) =>
    request<NormRule>(`/api/normalization/rules/${id}/disable`, {
      method: "POST",
    }),

  suggest: (data?: { min_corrections?: number; field_name?: string }) =>
    request<{
      suggested_rules: NormRule[];
      total_corrections_analyzed: number;
    }>("/api/normalization/suggest", {
      method: "POST",
      body: JSON.stringify(data ?? {}),
    }),

  apply: (documentId: string) =>
    request<{
      document_id: string;
      rules_applied: number;
      fields_modified: Array<{
        field_name: string;
        old_value: string;
        new_value: string;
        rule_id: string;
      }>;
    }>("/api/normalization/apply", {
      method: "POST",
      body: JSON.stringify({ document_id: documentId }),
    }),
};

// ── Tables & Export ─────────────────────────────────────────────────────────

export interface TableColumn {
  key: string;
  label: string;
  sortable: boolean;
  filterable: boolean;
  data_type: string;
}

export interface TableRow {
  id: string;
  data: Record<string, unknown>;
}

export interface TableQueryResponse {
  columns: TableColumn[];
  rows: TableRow[];
  total: number;
  offset: number;
  limit: number;
}

export interface TableFilter {
  column: string;
  operator: string;
  value: string | number | string[] | null;
}

export interface TableSort {
  column: string;
  direction: string;
}

export interface SavedView {
  id: string;
  name: string;
  table: string;
  columns: string[] | null;
  filters: TableFilter[];
  sort: TableSort[];
  is_shared: boolean;
  created_by: string | null;
  created_at: string;
}

export const tables = {
  query: (data: {
    table: string;
    filters?: TableFilter[];
    sort?: TableSort[];
    search?: string;
    offset?: number;
    limit?: number;
  }) =>
    request<TableQueryResponse>("/api/tables/query", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  exportUrl: (data: {
    table: string;
    filters?: TableFilter[];
    format?: string;
  }) => {
    // Returns blob for download
    return fetch(`${API_BASE}/api/tables/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  },

  export1cUrl: (data: { invoice_ids?: string[]; filters?: TableFilter[] }) => {
    return fetch(`${API_BASE}/api/tables/export-1c`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
  },

  listViews: (table = "invoices") =>
    request<SavedView[]>(`/api/tables/views?table=${table}`),

  createView: (data: {
    name: string;
    table: string;
    filters?: TableFilter[];
    sort?: TableSort[];
  }) =>
    request<SavedView>("/api/tables/views", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  deleteView: (id: string) =>
    request<{ status: string }>(`/api/tables/views/${id}`, {
      method: "DELETE",
    }),
};

// ── Health ───────────────────────────────────────────────────────────────────

export const health = {
  check: () =>
    request<{ status: string }>("/health").catch(() => ({
      status: "unavailable",
    })),
};
