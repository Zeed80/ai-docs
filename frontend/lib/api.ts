import { csrfHeaders } from "@/lib/auth";

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
    const val =
      process.env.NEXT_PUBLIC_API_URL?.trim() ||
      process.env.NEXT_PUBLIC_API_BASE_URL?.trim() ||
      "";
    return !val || val === "same-origin" ? "" : val;
  }
  return process.env.INTERNAL_API_URL ?? "http://127.0.0.1:8000";
}

const _MUTATION_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const isMutation = _MUTATION_METHODS.has(method);

  const response = await fetch(`${getApiBaseUrl()}${path}`, {
    ...init,
    credentials: "include",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(isMutation ? csrfHeaders() : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
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
