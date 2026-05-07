import { getApiBaseUrl as getSharedApiBaseUrl } from "@/lib/api-base";

export type ManufacturingCase = {
  id: string;
  title: string;
  description: string | null;
  customer_name: string | null;
  status: string;
  priority: string;
  created_at: string;
  updated_at: string;
  document_count: number;
};

export type DocumentArtifact = {
  id: string | null;
  artifact_type: string;
  storage_path: string;
  content_type: string | null;
  page_number: number | null;
  metadata: Record<string, unknown>;
};

export type WorkspaceDocument = {
  id: string;
  case_id: string;
  filename: string;
  content_type: string | null;
  sha256: string;
  size_bytes: number;
  status: string;
  document_type: string | null;
  extracted_text: string | null;
  extraction_result: Record<string, unknown> | null;
  ai_summary: string | null;
  artifacts: DocumentArtifact[];
  created_at: string;
  updated_at: string;
};

export type AuditEvent = {
  id: string;
  case_id: string | null;
  document_id: string | null;
  event_type: string;
  actor: string;
  message: string;
  created_at: string;
};

export type TaskJob = {
  id: string;
  task_type: string;
  status: string;
  case_id: string | null;
  document_id: string | null;
  result: Record<string, unknown>;
  error_message: string | null;
  created_at: string;
};

export type ApprovalGate = {
  id: string;
  case_id: string | null;
  action_id: string | null;
  gate_type: string;
  status: string;
  reason: string;
  payload: Record<string, unknown>;
  created_at: string;
  decided_at: string | null;
};

export type SignedFileUrl = {
  url: string;
  expires_at: number;
  filename: string;
  content_type: string | null;
};

export type CaseBundle = {
  item: ManufacturingCase;
  documents: WorkspaceDocument[];
  audit: AuditEvent[];
  approvals: ApprovalGate[];
  tasks: TaskJob[];
};

export type ChatSession = {
  id: string;
  title: string;
  user_key: string;
  created_at: string;
  updated_at: string;
  last_message_at: string | null;
};

export type ChatAttachment = {
  id: string;
  message_id: string | null;
  document_id: string | null;
  file_name: string;
  mime_type: string | null;
  size_bytes: number | null;
  created_at: string;
};

export type ChatHistoryMessage = {
  id: string;
  session_id: string;
  role: string;
  content: string | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
  attachments: ChatAttachment[];
};

/** Browser: same-origin `/api` via Next rewrites. SSR: direct backend or INTERNAL_API_URL. */
function getApiBaseUrl(): string {
  if (typeof window !== "undefined") {
    return getSharedApiBaseUrl();
  }
  return process.env.INTERNAL_API_URL ?? "http://127.0.0.1:8000";
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export async function listCases(): Promise<ManufacturingCase[]> {
  return apiFetch<ManufacturingCase[]>("/api/cases");
}

export async function getCaseBundle(caseId: string): Promise<CaseBundle> {
  const [item, documents, audit, approvals, tasks] = await Promise.all([
    apiFetch<ManufacturingCase>(`/api/cases/${caseId}`),
    apiFetch<WorkspaceDocument[]>(`/api/cases/${caseId}/documents`),
    apiFetch<AuditEvent[]>(`/api/cases/${caseId}/audit`),
    apiFetch<ApprovalGate[]>(`/api/approvals?case_id=${caseId}`),
    apiFetch<TaskJob[]>(`/api/tasks?case_id=${caseId}`),
  ]);
  return { item, documents, audit, approvals, tasks };
}

export async function createCase(payload: {
  title: string;
  description?: string;
  customer_name?: string;
  priority?: string;
}): Promise<ManufacturingCase> {
  return apiFetch<ManufacturingCase>("/api/cases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function uploadDocument(
  caseId: string,
  file: File,
): Promise<WorkspaceDocument> {
  const form = new FormData();
  form.append("file", file);
  return apiFetch<WorkspaceDocument>(`/api/cases/${caseId}/documents`, {
    method: "POST",
    body: form,
  });
}

export async function processDocument(documentId: string): Promise<TaskJob> {
  return apiFetch<TaskJob>(`/api/documents/${documentId}/process`, {
    method: "POST",
  });
}

export async function runTask(taskId: string): Promise<TaskJob> {
  return apiFetch<TaskJob>(`/api/tasks/${taskId}/run`, { method: "POST" });
}

export async function createDocumentDownloadUrl(
  documentId: string,
): Promise<SignedFileUrl> {
  return apiFetch<SignedFileUrl>(`/api/documents/${documentId}/download-url`, {
    method: "POST",
  });
}

export async function approveGate(
  gateId: string,
  reason: string,
): Promise<unknown> {
  return apiFetch(`/api/approvals/${gateId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actor: "frontend", reason }),
  });
}

export async function rejectGate(
  gateId: string,
  reason: string,
): Promise<unknown> {
  return apiFetch(`/api/approvals/${gateId}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actor: "frontend", reason }),
  });
}

export async function runAgentScenario(
  caseId: string,
  scenario: string,
): Promise<unknown> {
  return apiFetch(`/api/agent/scenarios/${scenario}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ case_id: caseId }),
  });
}

export async function analyzeDrawing(documentId: string): Promise<unknown> {
  return apiFetch(`/api/documents/${documentId}/drawing-analysis`, {
    method: "POST",
  });
}

export async function extractInvoice(documentId: string): Promise<unknown> {
  return apiFetch(`/api/documents/${documentId}/invoice-extraction`, {
    method: "POST",
  });
}

export async function listChatSessions(): Promise<ChatSession[]> {
  return apiFetch<ChatSession[]>("/api/chat/sessions");
}

export async function createChatSession(
  title = "Новый чат",
): Promise<ChatSession> {
  return apiFetch<ChatSession>("/api/chat/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export async function deleteChatSession(sessionId: string): Promise<void> {
  await apiFetch<void>(`/api/chat/sessions/${sessionId}`, { method: "DELETE" });
}

export async function getChatMessages(
  sessionId: string,
): Promise<ChatHistoryMessage[]> {
  return apiFetch<ChatHistoryMessage[]>(
    `/api/chat/sessions/${sessionId}/messages`,
  );
}
